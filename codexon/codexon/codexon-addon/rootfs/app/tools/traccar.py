from __future__ import annotations

import datetime as dt
import json
import os
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from tools.common import compact_json


TRACKERS = {
    "moto": {
        "label": "la moto",
        "traccar_name": "sinotrak",
        "entity_id": "device_tracker.sinotrak",
        "aliases": {"moto", "mi moto", "sinotrak", "gps moto", "gps de la moto", "localizador moto"},
    },
    "movil": {
        "label": "mi movil",
        "traccar_name": "samsung",
        "entity_id": "device_tracker.samsung",
        "aliases": {"movil", "mi movil", "telefono", "mi telefono", "samsung", "telefono samsung"},
    },
}


def fold_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(char for char in normalized if not unicodedata.combining(char)).strip()


def resolve_tracker(value: str) -> tuple[str, dict[str, Any]]:
    query = " ".join(fold_text(value).replace("_", " ").split())
    for key, tracker in TRACKERS.items():
        aliases = {fold_text(alias) for alias in tracker["aliases"]}
        if query == key or query in aliases:
            return key, tracker
    raise ValueError("Solo estan disponibles los localizadores 'moto' y 'movil'.")


def requested_tracker(value: str) -> str | None:
    text = " ".join(fold_text(value).split())
    location_terms = ("donde", "localiza", "localizar", "ubicacion", "posicion", "esta mi", "esta la")
    if not any(term in text for term in location_terms):
        return None
    matches = []
    for key, tracker in TRACKERS.items():
        if any(fold_text(alias) in text for alias in tracker["aliases"]):
            matches.append(key)
    return matches[0] if len(matches) == 1 else None


def _config_entry_paths() -> list[Path]:
    paths = []
    configured = os.getenv("TRACCAR_HA_CONFIG_ENTRIES")
    if configured:
        paths.append(Path(configured))
    paths.extend(
        [
            Path("/ha_config/.storage/core.config_entries"),
            Path("/config/.storage/core.config_entries"),
        ]
    )
    return paths


def _discover_ha_traccar_config() -> tuple[str | None, str | None]:
    for path in _config_entry_paths():
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries = payload.get("data", {}).get("entries", [])
            entry = next(item for item in entries if item.get("domain") == "traccar_server")
            data = entry.get("data") or {}
            scheme = "https" if data.get("ssl") else "http"
            base_url = f"{scheme}://{data['host']}:{data['port']}"
            token = str(data.get("api_token") or "").strip()
            if token:
                return base_url, token
        except (KeyError, OSError, TypeError, ValueError, StopIteration):
            continue
    return None, None


def traccar_connection(context: Any) -> tuple[str, str]:
    base_url = str(
        getattr(context, "traccar_base_url", None)
        or os.getenv("TRACCAR_BASE_URL")
        or os.getenv("TRACCAR_URL")
        or ""
    ).strip().rstrip("/")
    token = str(
        getattr(context, "traccar_api_token", None)
        or os.getenv("TRACCAR_API_TOKEN")
        or ""
    ).strip()
    if not base_url or not token:
        discovered_url, discovered_token = _discover_ha_traccar_config()
        base_url = base_url or str(discovered_url or "")
        token = token or str(discovered_token or "")
    if not base_url or not token:
        raise ValueError("Falta configurar TRACCAR_BASE_URL y TRACCAR_API_TOKEN.")
    return base_url, token


def parse_time(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def format_age(seconds: int | None) -> str:
    if seconds is None:
        return "antiguedad desconocida"
    if seconds < 60:
        return f"hace {seconds} segundos"
    if seconds < 3600:
        minutes = seconds // 60
        return f"hace {minutes} minuto" if minutes == 1 else f"hace {minutes} minutos"
    if seconds < 86400:
        hours = seconds // 3600
        return f"hace {hours} hora" if hours == 1 else f"hace {hours} horas"
    days = seconds // 86400
    return f"hace {days} dia" if days == 1 else f"hace {days} dias"


def _response_text(response: Any) -> str:
    text = str(getattr(response, "text", "") or "").strip()
    if text.startswith('"') and text.endswith('"'):
        try:
            return str(json.loads(text)).strip()
        except json.JSONDecodeError:
            pass
    return text


async def traccar_get_location(context: Any, args: dict[str, Any]) -> str:
    key, tracker = resolve_tracker(str(args.get("device") or args.get("query") or ""))
    base_url, token = traccar_connection(context)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    httpx = getattr(context, "httpx", None)
    if httpx is None:
        raise RuntimeError("httpx no esta disponible")

    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{base_url}/api/devices", headers=headers)
        response.raise_for_status()
        devices = response.json()
        device = next(
            (
                item
                for item in devices
                if fold_text(item.get("name")) == fold_text(tracker["traccar_name"])
            ),
            None,
        )
        if device is None:
            raise ValueError(f"Traccar no contiene el dispositivo permitido '{tracker['traccar_name']}'.")
        position_id = device.get("positionId")
        if not position_id:
            raise ValueError(f"Traccar no tiene posicion para {tracker['label']}.")

        response = await http.get(
            f"{base_url}/api/positions",
            headers=headers,
            params={"id": position_id},
        )
        response.raise_for_status()
        positions = response.json()
        if not positions:
            raise ValueError(f"Traccar no devolvio la posicion de {tracker['label']}.")
        position = positions[0]

        latitude = float(position["latitude"])
        longitude = float(position["longitude"])
        address = str(position.get("address") or "").strip()
        geocode_error = None
        try:
            response = await http.get(
                f"{base_url}/api/server/geocode",
                headers=headers,
                params={"latitude": latitude, "longitude": longitude},
            )
            response.raise_for_status()
            address = _response_text(response) or address
        except Exception as exc:  # La posicion sigue siendo util aunque falle el geocoder.
            geocode_error = f"{type(exc).__name__}: {exc}"

    fix_time = parse_time(position.get("fixTime") or position.get("deviceTime") or position.get("serverTime"))
    now = dt.datetime.now(dt.UTC)
    age_seconds = max(0, int((now - fix_time).total_seconds())) if fix_time else None
    is_current = bool(position.get("valid", True)) and age_seconds is not None and age_seconds <= 900
    freshness = "actual" if is_current else "reciente" if age_seconds is not None and age_seconds <= 7200 else "antigua"
    local_time = fix_time.astimezone(ZoneInfo("Europe/Madrid")) if fix_time else None
    attributes = position.get("attributes") or {}
    speed_kmh = round(float(position.get("speed") or 0) * 1.852, 1)
    map_url = f"https://www.google.com/maps?q={quote(str(latitude))},{quote(str(longitude))}"
    place = address or f"{latitude:.7f}, {longitude:.7f}"
    age_text = format_age(age_seconds)
    connection = fold_text(device.get("status") or "unknown")

    if is_current:
        recommended_answer = f"{tracker['label'].capitalize()} esta en {place}. Posicion recibida {age_text}."
    else:
        recommended_answer = (
            f"La ultima posicion conocida de {tracker['label']} es {place}, recibida {age_text}. "
            "No puedo asegurar que siga alli porque la posicion no es actual."
        )
    if connection == "offline":
        recommended_answer += " El localizador figura desconectado."
    if local_time:
        recommended_answer += f" Hora local de la posicion: {local_time.strftime('%d/%m/%Y %H:%M:%S')}."
    if attributes.get("motion") is False:
        recommended_answer += " Figuraba parado."
    if attributes.get("ignition") is False:
        recommended_answer += " Contacto apagado."
    recommended_answer += f" Mapa: {map_url}"

    return compact_json(
        {
            "device": key,
            "label": tracker["label"],
            "traccar_name": tracker["traccar_name"],
            "entity_id": tracker["entity_id"],
            "status": connection,
            "position_valid": bool(position.get("valid", True)),
            "is_current": is_current,
            "freshness": freshness,
            "age_seconds": age_seconds,
            "age_text": age_text,
            "fix_time_utc": fix_time.isoformat(timespec="seconds") if fix_time else None,
            "fix_time_local": local_time.isoformat(timespec="seconds") if local_time else None,
            "timezone": "Europe/Madrid",
            "latitude": latitude,
            "longitude": longitude,
            "address": address or None,
            "map_url": map_url,
            "speed_kmh": speed_kmh,
            "motion": attributes.get("motion"),
            "ignition": attributes.get("ignition"),
            "geocode_error": geocode_error,
            "recommended_answer": recommended_answer,
        },
        max_chars=10000,
    )
