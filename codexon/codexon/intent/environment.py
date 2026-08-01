from __future__ import annotations

import re
from typing import Any

from .text import normalize_text


def requested_environment_sensor(user_text: str, site_profile: Any | None = None) -> dict[str, Any] | None:
    folded = normalize_text(user_text)
    asks_temperature = any(term in folded for term in ("temperatura", "temp ", "grados", "calor", "frio"))
    asks_humidity = any(term in folded for term in ("humedad", "humedo", "húmedo"))
    if not asks_temperature and not asks_humidity:
        return None
    if asks_temperature and any(term in folded for term in ("agua", "termo", "calentador", "acumulador")):
        return _profile_sensor(site_profile, "environment.water_temperature", "temperatura del agua", "°C")
    if asks_humidity and any(term in folded for term in ("bano", "baño", "wc", "vater", "váter", "aseo", "termo")):
        return _profile_sensor(site_profile, "environment.bathroom_humidity", "humedad interior", "%")
    if asks_temperature and any(term in folded for term in ("casa", "cocina", "comedor", "salon", "salón", "interior", "dentro")):
        return _profile_sensor(site_profile, "environment.indoor_temperature", "temperatura interior", "°C")
    return None


def _profile_sensor(site_profile: Any | None, role: str, default_label: str, unit: str) -> dict[str, Any] | None:
    if site_profile is None:
        return None
    entities = list(site_profile.entities(role))
    if not entities:
        return None
    binding = site_profile.binding(role) or {}
    return {
        "entities": entities,
        "label": str(binding.get("label") or default_label),
        "unit": unit,
    }


def requested_climate_strategy(user_text: str) -> dict[str, Any] | None:
    folded = normalize_text(user_text)
    has_strategy_intent = any(
        term in folded
        for term in (
            "propongo", "propones", "proponer", "recomienda", "recomiendas",
            "estrategia", "mantener", "mantenga", "mantenerse", "objetivo",
            "tener", "dejar", "poner", "pondria", "pondrías", "conseguir",
        )
    )
    has_temperature_target = bool(
        re.search(r"\b\d+(?:[,.]\d+)?\s*(?:grados|º|°|o\b|c\b)", folded)
    )
    if not has_strategy_intent and not has_temperature_target:
        return None
    if not any(term in folded for term in ("casa", "interior", "dentro", "comedor", "salon", "salón", "cocina")):
        return None
    if not any(term in folded for term in ("temperatura", "grados", "º", "°", "calor", "frio", "fresco")) and not has_temperature_target:
        return None
    target_match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:grados|º|°|o\b|c\b)", folded)
    target = float(target_match.group(1).replace(",", ".")) if target_match else 25.0
    daytime = any(term in folded for term in ("dia", "día", "diurno", "mañana", "tarde"))
    return {"target_c": target, "daytime": daytime}
