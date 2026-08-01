from __future__ import annotations

from typing import Any

from .climate import environment_state_value


def format_light_sensation_answer(state_rows: list[tuple[str, dict[str, Any]]]) -> str:
    values: dict[str, tuple[float | None, str, str]] = {}
    for entity_id, state_data in state_rows:
        values[entity_id] = environment_state_value(state_data, "")
    interior = values.get("sensor.muralcocina_tsl2561_sensor_luz", (None, "-", "lx"))
    exterior = values.get("sensor.itorre692_solar_radiation", (None, "-", "W/m²"))
    interior_value, interior_text, interior_unit = interior
    exterior_value, exterior_text, exterior_unit = exterior
    if interior_value is None or exterior_value is None:
        return (
            "No pude comparar bien la sensación lumínica. "
            f"Interior: {interior_text}{interior_unit}; exterior: {exterior_text}{exterior_unit}."
        )
    if interior_value < 5:
        impression = "muy baja"
    elif interior_value < 30:
        impression = "baja"
    elif interior_value < 150:
        impression = "media"
    else:
        impression = "alta"
    return (
        f"La luz interior en cocina/comedor/salón es {interior_text}{interior_unit} "
        f"({impression}). Fuera/huerto hay {exterior_text}{exterior_unit} de radiación solar. "
        "Para medir el efecto de cerrar persianas/estores, habría que cerrar los covers indicados, esperar a que estabilice y volver a leer sensor.muralcocina_tsl2561_sensor_luz."
    )
