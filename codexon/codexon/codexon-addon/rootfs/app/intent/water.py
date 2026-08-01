from __future__ import annotations

from .text import normalize_text


WATER_TERMS: tuple[str, ...] = (
    "agua",
    "riego",
    "regar",
    "regando",
    "valvula",
    "válvula",
    "bomba",
    "caudal",
    "caudalimetro",
    "pulsometro",
    "contador",
    "litro",
    "litros",
    "fuga",
    "inundacion",
    "inundación",
    "aspersor",
    "goteo",
)

IRRIGATION_TERMS: tuple[str, ...] = (
    "riego",
    "regar",
    "regando",
    "zona",
    "zonas",
    "aspersor",
    "goteo",
)

WATER_METER_TERMS: tuple[str, ...] = (
    "agua",
    "caudal",
    "caudalimetro",
    "pulsometro",
    "contador",
    "litro",
    "litros",
)

WATER_ALARM_TERMS: tuple[str, ...] = (
    "fuga",
    "inundacion",
    "inundación",
    "sin agua",
    "alarma agua",
    "conflicto caudal",
)

WATER_ACTUATOR_TERMS: tuple[str, ...] = (
    "bomba",
    "valvula",
    "válvula",
)


def has_water_term(value: str) -> bool:
    folded = normalize_text(value)
    return any(term in folded for term in WATER_TERMS)


def has_irrigation_term(value: str) -> bool:
    folded = normalize_text(value)
    return any(term in folded for term in IRRIGATION_TERMS)


def water_role_hints(value: str) -> set[str]:
    folded = normalize_text(value)
    roles: set[str] = set()
    if any(term in folded for term in IRRIGATION_TERMS):
        roles.add("irrigation")
    if any(term in folded for term in WATER_METER_TERMS):
        roles.add("water_meter")
    if any(term in folded for term in WATER_ALARM_TERMS):
        roles.add("water_alarm")
    if any(term in folded for term in WATER_ACTUATOR_TERMS):
        roles.add("water_actuator")
    return roles


def infer_water_entity_roles(
    entity_text: str,
    *,
    domain: str = "",
    device_class: str = "",
    unit: str = "",
) -> list[tuple[str, str, str]]:
    folded = normalize_text(entity_text)
    normalized_device_class = normalize_text(device_class)
    normalized_unit = normalize_text(unit).lower()
    roles: list[tuple[str, str, str]] = []

    if any(term in folded for term in IRRIGATION_TERMS):
        roles.append(("irrigation", "water", "riego, zonas y bombas de riego"))
    if (
        any(term in folded for term in WATER_METER_TERMS)
        or normalized_device_class == "water"
        or normalized_unit in {"l", "l/min", "m3", "m³"}
    ):
        roles.append(("water_meter", "water", "contador, caudal o consumo de agua"))
    if any(term in folded for term in WATER_ALARM_TERMS):
        roles.append(("water_alarm", "water", "alarma de fuga, inundacion o caudal de agua"))
    if any(term in folded for term in WATER_ACTUATOR_TERMS):
        roles.append(("water_actuator", "water", "bomba o valvula de agua"))

    return roles
