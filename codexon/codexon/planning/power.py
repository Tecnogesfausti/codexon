from __future__ import annotations

import re
from typing import Any

from intent.text import normalize_text
from .climate import environment_state_value


def format_power_supply_status_answer(state_rows: list[tuple[str, dict[str, Any]]]) -> str:
    voltages: list[tuple[str, float, str]] = []
    powers: list[tuple[str, float, str]] = []
    ups_status = ""
    ups_status_data = ""
    ups_battery = ""
    ups_load = ""
    unavailable: list[str] = []
    for entity_id, state_data in state_rows:
        raw_state = state_data.get("state")
        if isinstance(raw_state, dict):
            raw_state = raw_state.get("state")
        text_state = str(raw_state or "").strip()
        if entity_id == "sensor.myups_status":
            ups_status = text_state
        elif entity_id == "sensor.myups_status_data":
            ups_status_data = text_state
        elif entity_id == "sensor.myups_battery_charge":
            ups_battery = text_state
        elif entity_id == "sensor.myups_load":
            ups_load = text_state
        numeric, value, unit = environment_state_value(state_data, "")
        if numeric is None:
            unavailable.append(entity_id)
            continue
        if "voltage" in entity_id:
            voltages.append((entity_id, numeric, unit or "V"))
        elif "power" in entity_id or "potencia" in entity_id:
            powers.append((entity_id, numeric, unit or "W"))

    ups_folded = normalize_text(f"{ups_status} {ups_status_data}")
    ups_problem_terms = ("on battery", "battery", "bateria", "ob", "offline", "falla", "fault", "low battery", "lb")
    ups_online_terms = ("online", "ol")
    ups_details = []
    if ups_status:
        ups_details.append(f"UPS {ups_status}")
    if ups_status_data:
        ups_details.append(f"datos {ups_status_data}")
    if ups_battery:
        ups_details.append(f"batería {ups_battery}%")
    if ups_load:
        ups_details.append(f"carga {ups_load}%")
    ups_summary = ", ".join(ups_details)
    if any(re.search(rf"\b{re.escape(term)}\b", ups_folded) for term in ups_problem_terms):
        suffix = f" ({ups_summary})" if ups_summary else ""
        return (
            "Problema grave de suministro: la UPS indica funcionamiento en batería o fallo"
            f"{suffix}. Sin red eléctrica pueden quedar afectados nevera y riego; conviene enviar alerta móvil y revisar cargas críticas."
        )

    live_voltages = [row for row in voltages if row[1] >= 180]
    live_power = [row for row in powers if row[1] > 1]
    if any(re.search(rf"\b{re.escape(term)}\b", ups_folded) for term in ups_online_terms):
        details = ", ".join(f"{entity_id} {value:.1f}{unit}" for entity_id, value, unit in live_voltages[:3])
        suffix = f" Además hay tensión ({details})." if details else ""
        return f"No parece que se haya ido la luz: la UPS está online ({ups_summary}).{suffix}"
    if live_voltages:
        details = ", ".join(f"{entity_id} {value:.1f}{unit}" for entity_id, value, unit in live_voltages[:4])
        return f"No parece que se haya ido la luz: hay tensión eléctrica ({details})."
    if live_power:
        details = ", ".join(f"{entity_id} {value:.1f}{unit}" for entity_id, value, unit in live_power[:4])
        return f"No parece un corte general: todavía hay consumo medido ({details}), aunque no pude confirmar tensión."
    if voltages or powers:
        details = ", ".join(f"{entity_id} {value:.1f}{unit}" for entity_id, value, unit in (voltages + powers)[:4])
        return f"Podría haber ausencia de suministro o los medidores no están viendo tensión/consumo. Lecturas: {details}."
    return "No pude comprobar si se ha ido la luz: no obtuve lecturas válidas de tensión o potencia."
