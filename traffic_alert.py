#!/usr/bin/env python3
"""
traffic_alert.py — Consulta tráfico en tiempo real entre dos ubicaciones fijas
y devuelve un resumen compacto listo para enviar por Telegram u otro sistema.

Proveedores soportados:
  - google  : Google Maps Routes API (v2)
  - tomtom  : TomTom Routing API (v1)

Salida:
  - Línea JSON compacta por stdout con todos los datos calculados.
  - Línea de resumen humano legible.

Uso:
  python3 traffic_alert.py

Configuración mediante fichero .env (opcional):
  Crea un fichero ".env" en el mismo directorio que el script con el formato:
    API_KEY=tu_clave_aqui
    ORIGIN_LAT=40.453054
  Las variables de entorno del sistema tienen prioridad sobre el .env.

Variables de entorno (pueden sobreescribir los valores por defecto del script):
  ORIGIN_LAT              Latitud origen
  ORIGIN_LON              Longitud origen
  DEST_LAT                Latitud destino
  DEST_LON                Longitud destino
  WAYPOINT_LAT            Latitud punto intermedio (opcional)
  WAYPOINT_LON            Longitud punto intermedio (opcional)
  API_KEY                 Clave de la API (Google o TomTom)
  PROVIDER                "google" | "tomtom"
  DELAY_THRESHOLD_MINUTES Minutos de retraso a partir de los cuales se alerta (por defecto: 10)

Ejemplo cron (lunes a viernes a las 17:30):
  30 17 * * 1-5 /usr/bin/python3 /ruta/traffic_alert.py >> /var/log/traffic.log 2>&1

Ejemplo desde un agente que manda el resultado por Telegram:
  import subprocess, json, requests as req

  result = subprocess.run(["python3", "traffic_alert.py"], capture_output=True, text=True)
  data = json.loads(result.stdout.splitlines()[0])   # primera línea = JSON
  summary = result.stdout.splitlines()[1]            # segunda línea = resumen humano

  req.post(
      f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
      json={"chat_id": CHAT_ID, "text": summary},
  )
"""

import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import Literal, Optional

import requests


# ---------------------------------------------------------------------------
# Carga de .env (si existe) — las variables de entorno ya definidas tienen
# prioridad; el .env solo rellena las que faltan.
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
    """Lee un fichero .env y aplica las variables que aún no estén en el entorno."""
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass  # .env opcional; no es un error


_load_dotenv()

# ---------------------------------------------------------------------------
# Configuración por defecto — sobreescribible mediante variables de entorno
# ---------------------------------------------------------------------------

DEFAULT_ORIGIN_LAT: float = 40.453054          # Ejemplo: oficina en Madrid
DEFAULT_ORIGIN_LON: float = -3.688344
DEFAULT_DEST_LAT: float = 40.416775            # Ejemplo: casa en Madrid
DEFAULT_DEST_LON: float = -3.703790
DEFAULT_WAYPOINT_LAT: Optional[float] = None   # Punto intermedio opcional
DEFAULT_WAYPOINT_LON: Optional[float] = None
DEFAULT_API_KEY: str = ""                      # Obligatorio: pon tu clave aquí o en env
DEFAULT_PROVIDER: Literal["google", "tomtom"] = "google"
DEFAULT_DELAY_THRESHOLD_MINUTES: int = 10

# ---------------------------------------------------------------------------
# Tipos y estructuras de datos
# ---------------------------------------------------------------------------

Provider = Literal["google", "tomtom"]
Status = Literal["OK", "ALERTA"]


@dataclass
class Config:
    origin_lat: float
    origin_lon: float
    dest_lat: float
    dest_lon: float
    waypoint_lat: Optional[float]
    waypoint_lon: Optional[float]
    api_key: str
    provider: Provider
    delay_threshold_minutes: int


@dataclass
class TrafficResult:
    provider: str
    origin: str
    destination: str
    duration_now_min: int          # Tiempo actual con tráfico (minutos)
    duration_base_min: Optional[int]  # Tiempo base sin tráfico (minutos, si disponible)
    delay_min: Optional[int]          # Diferencia en minutos (now - base)
    status: Status                 # "OK" o "ALERTA"
    summary: str                   # Línea de texto lista para Telegram


# ---------------------------------------------------------------------------
# Lectura de configuración
# ---------------------------------------------------------------------------

def load_config() -> Config:
    """Lee la configuración desde variables de entorno o valores por defecto."""

    def _get_float(key: str, default: float) -> float:
        val = os.environ.get(key)
        if val is not None:
            try:
                return float(val)
            except ValueError:
                _die(f"Variable de entorno inválida: {key}={val!r} (se espera un número)")
        return default

    def _get_int(key: str, default: int) -> int:
        val = os.environ.get(key)
        if val is not None:
            try:
                return int(val)
            except ValueError:
                _die(f"Variable de entorno inválida: {key}={val!r} (se espera un entero)")
        return default

    provider_raw = os.environ.get("PROVIDER", DEFAULT_PROVIDER).lower()
    if provider_raw not in ("google", "tomtom"):
        _die(f"PROVIDER no soportado: {provider_raw!r}. Usa 'google' o 'tomtom'.")

    api_key = os.environ.get("API_KEY", DEFAULT_API_KEY).strip()
    if not api_key:
        _die("API_KEY no configurada. Defínela como variable de entorno o en el script.")

    # Waypoint: ambas coordenadas deben estar presentes o ninguna
    waypoint_lat = _get_float("WAYPOINT_LAT", DEFAULT_WAYPOINT_LAT) if os.environ.get("WAYPOINT_LAT") or DEFAULT_WAYPOINT_LAT is not None else None
    waypoint_lon = _get_float("WAYPOINT_LON", DEFAULT_WAYPOINT_LON) if os.environ.get("WAYPOINT_LON") or DEFAULT_WAYPOINT_LON is not None else None
    if (waypoint_lat is None) != (waypoint_lon is None):
        _die("WAYPOINT_LAT y WAYPOINT_LON deben definirse juntos o no definirse ninguno.")

    return Config(
        origin_lat=_get_float("ORIGIN_LAT", DEFAULT_ORIGIN_LAT),
        origin_lon=_get_float("ORIGIN_LON", DEFAULT_ORIGIN_LON),
        dest_lat=_get_float("DEST_LAT", DEFAULT_DEST_LAT),
        dest_lon=_get_float("DEST_LON", DEFAULT_DEST_LON),
        waypoint_lat=waypoint_lat,
        waypoint_lon=waypoint_lon,
        api_key=api_key,
        provider=provider_raw,  # type: ignore[arg-type]
        delay_threshold_minutes=_get_int("DELAY_THRESHOLD_MINUTES", DEFAULT_DELAY_THRESHOLD_MINUTES),
    )


# ---------------------------------------------------------------------------
# Proveedor: Google Maps Routes API (v2)
# Documentación: https://developers.google.com/maps/documentation/routes
#
# Campos usados:
#   - routes[0].duration          → duración actual con tráfico (segundos)
#   - routes[0].staticDuration    → duración sin tráfico (segundos)
#     Si staticDuration no está presente, se usa duration como base.
# ---------------------------------------------------------------------------

GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

def fetch_google(cfg: Config) -> TrafficResult:
    """Consulta Google Maps Routes API y devuelve un TrafficResult."""

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": cfg.api_key,
        # Solicitamos los campos mínimos necesarios para no incurrir en costes extra
        "X-Goog-FieldMask": "routes.duration,routes.staticDuration",
    }

    payload = {
        "origin": {
            "location": {
                "latLng": {"latitude": cfg.origin_lat, "longitude": cfg.origin_lon}
            }
        },
        "destination": {
            "location": {
                "latLng": {"latitude": cfg.dest_lat, "longitude": cfg.dest_lon}
            }
        },
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",   # activa el tráfico en tiempo real
        "computeAlternativeRoutes": False,
    }

    if cfg.waypoint_lat is not None and cfg.waypoint_lon is not None:
        payload["intermediates"] = [
            {
                "location": {
                    "latLng": {"latitude": cfg.waypoint_lat, "longitude": cfg.waypoint_lon}
                }
            }
        ]

    try:
        resp = requests.post(GOOGLE_ROUTES_URL, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        _die("Error de red al conectar con Google Routes API.")
    except requests.exceptions.Timeout:
        _die("Timeout al conectar con Google Routes API.")
    except requests.exceptions.HTTPError as exc:
        _die(f"Google Routes API devolvió error HTTP {exc.response.status_code}: {exc.response.text[:200]}")

    data = _parse_json(resp, "Google Routes API")

    try:
        route = data["routes"][0]
    except (KeyError, IndexError):
        _die(f"Google Routes API: respuesta inesperada, sin rutas. Respuesta: {data}")

    # duration es una cadena tipo "1234s"
    duration_now_sec = _parse_google_duration(route.get("duration", ""))
    duration_base_sec = _parse_google_duration(route.get("staticDuration", ""))

    duration_now_min = _sec_to_min(duration_now_sec)
    duration_base_min = _sec_to_min(duration_base_sec) if duration_base_sec else None

    return _build_result(cfg, "google", duration_now_min, duration_base_min)


def _parse_google_duration(value: str) -> Optional[int]:
    """Convierte la cadena de duración de Google ('123s') a segundos enteros."""
    if not value:
        return None
    try:
        return int(value.rstrip("s"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Proveedor: TomTom Routing API (v1)
# Documentación: https://developer.tomtom.com/routing-api/documentation/routing/calculate-route
#
# Campos usados:
#   - routes[0].summary.travelTimeInSeconds       → duración con tráfico
#   - routes[0].summary.noTrafficTravelTimeInSeconds → duración sin tráfico
#     Si no está presente (viajes muy cortos), se usa historicTrafficTravelTimeInSeconds
#     o directamente travelTimeInSeconds como base.
# ---------------------------------------------------------------------------

TOMTOM_ROUTING_URL = (
    "https://api.tomtom.com/routing/1/calculateRoute"
    "/{origin}:{destination}/json"
)

def fetch_tomtom(cfg: Config) -> TrafficResult:
    """Consulta TomTom Routing API y devuelve un TrafficResult."""

    origin = f"{cfg.origin_lat},{cfg.origin_lon}"
    destination = f"{cfg.dest_lat},{cfg.dest_lon}"

    if cfg.waypoint_lat is not None and cfg.waypoint_lon is not None:
        waypoint = f"{cfg.waypoint_lat},{cfg.waypoint_lon}"
        # TomTom: los waypoints van entre origen y destino separados por ':'
        route_points = f"{origin}:{waypoint}:{destination}"
    else:
        route_points = f"{origin}:{destination}"

    url = TOMTOM_ROUTING_URL.format(origin=origin, destination=destination).replace(
        f"{origin}:{destination}", route_points
    )

    params = {
        "key": cfg.api_key,
        "travelMode": "car",
        "traffic": "true",          # activa tráfico en tiempo real
        "routeType": "fastest",
        "computeTravelTimeFor": "all",   # devuelve noTrafficTravelTimeInSeconds
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        _die("Error de red al conectar con TomTom Routing API.")
    except requests.exceptions.Timeout:
        _die("Timeout al conectar con TomTom Routing API.")
    except requests.exceptions.HTTPError as exc:
        _die(f"TomTom Routing API devolvió error HTTP {exc.response.status_code}: {exc.response.text[:200]}")

    data = _parse_json(resp, "TomTom Routing API")

    try:
        summary = data["routes"][0]["summary"]
    except (KeyError, IndexError):
        _die(f"TomTom Routing API: respuesta inesperada, sin rutas. Respuesta: {data}")

    duration_now_sec: int = summary.get("travelTimeInSeconds", 0)

    # Preferimos noTrafficTravelTimeInSeconds; si no existe, usamos historicTrafficTravelTimeInSeconds
    duration_base_sec: Optional[int] = (
        summary.get("noTrafficTravelTimeInSeconds")
        or summary.get("historicTrafficTravelTimeInSeconds")
    )

    duration_now_min = _sec_to_min(duration_now_sec)
    duration_base_min = _sec_to_min(duration_base_sec) if duration_base_sec else None

    return _build_result(cfg, "tomtom", duration_now_min, duration_base_min)


# ---------------------------------------------------------------------------
# Lógica de negocio compartida
# ---------------------------------------------------------------------------

def _build_result(
    cfg: Config,
    provider: str,
    duration_now_min: int,
    duration_base_min: Optional[int],
) -> TrafficResult:
    """Calcula la diferencia, el estado y construye el mensaje de resumen."""

    delay_min: Optional[int] = None
    if duration_base_min is not None:
        delay_min = duration_now_min - duration_base_min

    # Determinar estado
    if delay_min is not None and delay_min >= cfg.delay_threshold_minutes:
        status: Status = "ALERTA"
    else:
        status = "OK"

    # Construir mensaje legible
    origin_label = f"{cfg.origin_lat},{cfg.origin_lon}"
    dest_label = f"{cfg.dest_lat},{cfg.dest_lon}"
    if cfg.waypoint_lat is not None and cfg.waypoint_lon is not None:
        waypoint_label = f"{cfg.waypoint_lat},{cfg.waypoint_lon}"
        prefix = f"Tráfico {origin_label}→{waypoint_label}→{dest_label}"
    else:
        prefix = f"Tráfico {origin_label}→{dest_label}"

    if duration_base_min is not None and delay_min is not None:
        sign = "+" if delay_min >= 0 else ""
        if status == "ALERTA":
            tail = "Hay retención importante."
        else:
            tail = "Situación normal."
        summary = (
            f"{prefix}: {duration_base_min} min normalmente, "
            f"{duration_now_min} min ahora ({sign}{delay_min}). {tail}"
        )
    else:
        # La API no devolvió duración base; solo mostramos el tiempo actual
        if status == "ALERTA":
            tail = "Posible retención (no hay datos base para comparar)."
        else:
            tail = "Situación aparentemente normal."
        summary = f"{prefix}: {duration_now_min} min ahora. {tail}"

    return TrafficResult(
        provider=provider,
        origin=origin_label,
        destination=dest_label,
        duration_now_min=duration_now_min,
        duration_base_min=duration_base_min,
        delay_min=delay_min,
        status=status,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _sec_to_min(seconds: Optional[int]) -> Optional[int]:
    """Convierte segundos a minutos redondeando al entero más cercano."""
    if seconds is None:
        return None
    return round(seconds / 60)


def _parse_json(resp: requests.Response, context: str) -> dict:
    """Parsea JSON de la respuesta; termina con error si falla."""
    try:
        return resp.json()
    except ValueError:
        _die(f"{context}: respuesta no es JSON válido. Contenido: {resp.text[:200]}")


def _die(message: str) -> None:
    """Imprime un error por stderr y termina el proceso con código 1."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()

    # Despachar al proveedor correcto
    if cfg.provider == "google":
        result = fetch_google(cfg)
    elif cfg.provider == "tomtom":
        result = fetch_tomtom(cfg)
    else:
        _die(f"Proveedor no soportado: {cfg.provider!r}")

    # Salida 1: JSON compacto (útil para pipelines / agentes)
    output = asdict(result)
    print(json.dumps(output, ensure_ascii=False))

    # Salida 2: resumen humano (útil para Telegram)
    print(result.summary)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# EJEMPLOS DE USO
# ---------------------------------------------------------------------------
#
# 1. Ejecución desde consola (Google):
#
#    export API_KEY="AIzaSy..."
#    export PROVIDER="google"
#    export ORIGIN_LAT="40.453054"
#    export ORIGIN_LON="-3.688344"
#    export DEST_LAT="40.416775"
#    export DEST_LON="-3.703790"
#    export WAYPOINT_LAT="40.435000"   # opcional: punto intermedio (ej: salida A-6)
#    export WAYPOINT_LON="-3.695000"
#    export DELAY_THRESHOLD_MINUTES="10"
#    python3 traffic_alert.py
#
#    Salida esperada:
#      {"provider": "google", "origin": "40.453054,-3.688344", ...}
#      Tráfico 40.453054,-3.688344→40.416775,-3.703790: 24 min normalmente, 39 min ahora (+15). Hay retención importante.
#
# 2. Ejecución desde consola (TomTom):
#
#    export API_KEY="abcd1234..."
#    export PROVIDER="tomtom"
#    python3 traffic_alert.py
#
# 3. Ejemplo de cron (lunes a viernes a las 17:30):
#
#    30 17 * * 1-5 \
#      API_KEY="AIzaSy..." \
#      PROVIDER="google" \
#      ORIGIN_LAT="40.453054" ORIGIN_LON="-3.688344" \
#      DEST_LAT="40.416775"   DEST_LON="-3.703790" \
#      /usr/bin/python3 /home/user/scripts/traffic_alert.py \
#      >> /var/log/traffic.log 2>&1
#
# 4. Llamada desde un agente Claude / OpenClaw que luego manda el resultado por Telegram:
#
#    import subprocess, json
#    import requests as req
#
#    BOT_TOKEN = "123456:ABC-..."
#    CHAT_ID   = "987654321"
#
#    proc = subprocess.run(
#        ["python3", "/home/user/scripts/traffic_alert.py"],
#        capture_output=True, text=True,
#        env={**os.environ, "API_KEY": "...", "PROVIDER": "google"},
#    )
#
#    if proc.returncode != 0:
#        # Reenviar el error al chat de Telegram
#        req.post(
#            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
#            json={"chat_id": CHAT_ID, "text": f"⚠️ Error en traffic_alert:\n{proc.stderr}"},
#        )
#    else:
#        lines = proc.stdout.strip().splitlines()
#        data    = json.loads(lines[0])   # JSON compacto con todos los datos
#        summary = lines[1]               # Línea de texto lista para Telegram
#
#        # Opcional: añadir emoji según el estado
#        emoji = "🔴" if data["status"] == "ALERTA" else "🟢"
#        req.post(
#            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
#            json={"chat_id": CHAT_ID, "text": f"{emoji} {summary}"},
#        )
