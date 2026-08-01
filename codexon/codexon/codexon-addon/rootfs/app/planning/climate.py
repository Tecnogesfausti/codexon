from __future__ import annotations

from typing import Any


def environment_state_value(state_data: dict[str, Any], fallback_unit: str) -> tuple[float | None, str, str]:
    raw_state = state_data.get("state")
    attrs = state_data.get("attributes") or {}
    if isinstance(state_data.get("state"), dict):
        nested = state_data["state"]
        raw_state = nested.get("state")
        attrs = nested.get("attributes") or {}
    unit = str(attrs.get("unit_of_measurement") or fallback_unit or "").strip()
    try:
        numeric = float(raw_state)
        value = f"{numeric:.1f}".replace(".", ",")
    except (TypeError, ValueError):
        numeric = None
        value = str(raw_state)
    return numeric, value, unit


def format_environment_sensor_answer(label: str, state_rows: list[tuple[str, dict[str, Any]]], fallback_unit: str) -> str:
    readings: list[dict[str, Any]] = []
    for entity_id, state_data in state_rows:
        numeric, value, unit = environment_state_value(state_data, fallback_unit)
        readings.append({"entity_id": entity_id, "numeric": numeric, "value": value, "unit": unit or fallback_unit})
    numeric_readings = [row for row in readings if row["numeric"] is not None]
    if len(numeric_readings) >= 2:
        low = min(numeric_readings, key=lambda row: row["numeric"])
        high = max(numeric_readings, key=lambda row: row["numeric"])
        details = ", ".join(f"{row['entity_id']} {row['value']}{row['unit']}" for row in numeric_readings)
        return f"La {label} está entre {low['value']}{low['unit']} y {high['value']}{high['unit']} ({details})."
    if readings:
        row = readings[0]
        return f"La {label} es {row['value']}{row['unit']} ({row['entity_id']})."
    return f"No pude leer la {label}."


def format_climate_strategy_answer(
    *,
    target_c: float,
    daytime: bool,
    temp_rows: list[tuple[str, dict[str, Any]]],
    light_rows: list[tuple[str, dict[str, Any]]],
    ac_state: dict[str, Any],
    power_state: dict[str, Any],
    pvpc: dict[str, Any],
    climate_control_entity: str,
    power_sensor_entity: str | None,
    indoor_light_entity: str | None,
    outdoor_radiation_entity: str | None,
) -> str:
    temps = [environment_state_value(state, "°C")[0] for _, state in temp_rows]
    valid_temps = [value for value in temps if value is not None]
    current_avg = sum(valid_temps) / len(valid_temps) if valid_temps else None
    temp_detail = ", ".join(
        f"{entity_id} {environment_state_value(state, '°C')[1]}{environment_state_value(state, '°C')[2]}"
        for entity_id, state in temp_rows
    )
    interior_light = next(
        (environment_state_value(state, "") for entity_id, state in light_rows if entity_id == indoor_light_entity),
        (None, "-", "lx"),
    )
    exterior_rad = next(
        (environment_state_value(state, "") for entity_id, state in light_rows if entity_id == outdoor_radiation_entity),
        (None, "-", "W/m²"),
    )
    power_value = environment_state_value(power_state, "W")
    ac_mode = str(ac_state.get("state") or "unknown")
    cheapest = pvpc.get("cheapest_future_hours") or pvpc.get("cheapest_hours") or []
    cheapest_text = ", ".join(
        f"{row.get('range')} ({float(row.get('cent_kwh') or 0):.2f} c/kWh)"
        for row in cheapest[:3]
        if isinstance(row, dict)
    ) or "no pude leer franjas baratas"
    if current_avg is None:
        status = "No pude calcular bien la temperatura media interior."
    elif current_avg < target_c - 0.5:
        status = f"Ahora está por debajo del objetivo: media {current_avg:.1f}°C frente a {target_c:.1f}°C. No encendería el AC todavía."
    elif current_avg <= target_c + 0.5:
        status = f"Ahora está cerca del objetivo: media {current_avg:.1f}°C frente a {target_c:.1f}°C. Mantendría vigilancia sin forzar."
    else:
        status = f"Ahora está por encima del objetivo: media {current_avg:.1f}°C frente a {target_c:.1f}°C. Enfriaría si la tendencia sigue subiendo."
    window = "durante el día" if daytime else "en la ventana que indiques"
    return (
        f"{status}\n"
        f"Lecturas usadas: {temp_detail}. Climatización: {climate_control_entity} está en {ac_mode}; potencia {power_value[1]}{power_value[2]}.\n"
        f"Luz/radiación: interior {interior_light[1]}{interior_light[2]}, exterior {exterior_rad[1]}{exterior_rad[2]}.\n"
        f"Propuesta para mantener {target_c:.1f}°C {window}:\n"
        f"- Vigilar {', '.join(entity_id for entity_id, _ in temp_rows)} y usar la media como referencia.\n"
        f"- Encender {climate_control_entity} solo si la media supera {target_c + 0.5:.1f}°C durante varios minutos o sube rápido.\n"
        f"- Apagarlo cuando baje a {target_c - 0.2:.1f}°C"
        f"{f' o si {power_sensor_entity} no confirma consumo' if power_sensor_entity else ''}.\n"
        f"- Si hay mucha radiación exterior, valorar cerrar las cubiertas configuradas antes de gastar en climatización.\n"
        f"- Priorizar enfriamiento en horas PVPC baratas: {cheapest_text}.\n"
        "No lo agendo todavía. Si quieres, te preparo el plan con horarios, umbrales, presupuesto eléctrico y confirmación final antes de crear tareas."
    )
