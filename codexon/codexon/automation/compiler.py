from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

from automation.schema import validate_plan


EntityResolver = Callable[[str], tuple[str, str] | None]
SensorResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class CompiledAutomation:
    delay_seconds: float
    title: str
    summary: str
    plan: dict


def compile_numeric_condition(
    user_text: str,
    *,
    resolve_sensor: SensorResolver,
    resolve_action: EntityResolver,
) -> CompiledAutomation | None:
    folded = _fold(user_text.strip())
    if not re.search(r"\b(cuando|si)\b", folded):
        return None
    match = re.search(
        r"\b(?:cuando|si)\b\s+(?P<condition>.+?)\s+"
        r"(?P<op>mayord|mayor|mas|superior|por encima|encima|menor|inferior|por debajo|debajo)"
        r"(?:\s+que|\s+de|\s+a)?\s+"
        r"(?P<threshold>-?\d+(?:[,.]\d+)?)\s*(?P<action>.*)$",
        folded,
    )
    if not match:
        return None
    condition_text = match.group("condition").strip()
    action_text = match.group("action").strip()
    if not action_text:
        return None
    query = re.sub(r"\b(la|el|los|las|de|del|sea|este|esta|temperatura)\b", " ", condition_text)
    query = re.sub(r"\s+", " ", query).strip() or condition_text
    sensor_entity = resolve_sensor(query)
    action = resolve_action(action_text)
    if sensor_entity is None or action is None:
        return None
    action_entity, action_label = action
    if any(word in action_text for word in ("enciende", "encender", "activa", "activar")):
        action_service, expected_state = "turn_on", "on"
    elif any(word in action_text for word in ("apaga", "apagar", "desactiva", "desactivar")):
        action_service, expected_state = "turn_off", "off"
    else:
        return None
    op_text = match.group("op")
    operator = "gt" if op_text in {"mayord", "mayor", "mas", "superior", "por encima", "encima"} else "lt"
    symbol = ">" if operator == "gt" else "<"
    threshold = float(match.group("threshold").replace(",", "."))
    plan = validate_plan(
        {
            "version": 1,
            "name": f"Condicion {sensor_entity} {symbol} {threshold:g}",
            "conditions": [{"entity_id": sensor_entity, "operator": operator, "value": threshold}],
            "condition_policy": {"on_false": "reschedule", "delay_seconds": 120},
            "steps": [
                {
                    "type": "service",
                    "domain": action_entity.split(".", 1)[0],
                    "service": action_service,
                    "target": {"entity_id": action_entity},
                    "expected_state": expected_state,
                }
            ],
        }
    )
    return CompiledAutomation(
        delay_seconds=60,
        title=f"Condicion: {condition_text} {symbol} {threshold:g}",
        summary=(
            f"comprobara {sensor_entity}; si el valor es {symbol} {threshold:g} ejecutara "
            f"{action_service} sobre {action_label} ({action_entity}); si no, reintentara en 120 segundos"
        ),
        plan=plan,
    )


def compile_binary_transition(
    user_text: str,
    *,
    start_time: str,
    resolve_sensor: SensorResolver,
    resolve_action: EntityResolver,
) -> CompiledAutomation | None:
    folded = _fold(user_text.strip())
    match = re.search(
        r"\b(?:cuando|si)\b\s+(?P<condition>.+?)\s+"
        r"(?P<action>(?:enciende|encender|apaga|apagar|activa|activar|desactiva|desactivar)\b.*)$",
        folded,
    )
    if not match:
        return None
    condition_text = match.group("condition").strip()
    action_text = match.group("action").strip()
    off_markers = ("desactive", "desactivar", "inactivo", "inactive", "cierre", "cerrar", "pase a off")
    on_markers = (
        "active", "activar", "abierto", "abra", "apertura", "pulse", "pulsar", "llamen", "llame",
        "presencia", "movimiento", "detecte", "pase a on",
    )
    if any(marker in condition_text for marker in off_markers):
        from_state, to_state = "on", "off"
    elif any(marker in condition_text for marker in on_markers):
        from_state, to_state = "off", "on"
    else:
        return None
    query = re.sub(
        r"\b(el|la|los|las|sensor|se|este|esta|active|activar|desactive|desactivar|abierto|abra|apertura|"
        r"pulse|pulsar|llamen|llame|presencia|movimiento|detecte|inactivo|inactive|cierre|cerrar|pase|a|on|off)\b",
        " ",
        condition_text,
    )
    query = re.sub(r"\s+", " ", query).strip() or condition_text
    sensor_entity = resolve_sensor(query)
    action = resolve_action(action_text)
    if sensor_entity is None or action is None:
        return None
    action_entity, action_label = action
    if any(word in action_text for word in ("enciende", "encender", "activa", "activar")):
        action_service, expected_state = "turn_on", "on"
    elif any(word in action_text for word in ("apaga", "apagar", "desactiva", "desactivar")):
        action_service, expected_state = "turn_off", "off"
    else:
        return None
    plan = validate_plan(
        {
            "version": 1,
            "name": f"Transicion {sensor_entity} {from_state}->{to_state}",
            "conditions": [
                {
                    "type": "transition",
                    "entity_id": sensor_entity,
                    "from_state": from_state,
                    "to_state": to_state,
                    "after": start_time,
                    "settle_seconds": 5,
                    "operator": "gte",
                    "value": 1,
                }
            ],
            "condition_policy": {"on_false": "reschedule", "delay_seconds": 5},
            "steps": [
                {
                    "type": "service",
                    "domain": action_entity.split(".", 1)[0],
                    "service": action_service,
                    "target": {"entity_id": action_entity},
                    "expected_state": expected_state,
                }
            ],
        }
    )
    return CompiledAutomation(
        delay_seconds=1,
        title=f"Cuando {sensor_entity} cambie {from_state}->{to_state}",
        summary=(
            f"vigilara todas las transiciones {from_state}->{to_state} de {sensor_entity} desde {start_time}; "
            f"cuando encuentre una ejecutara {action_service} sobre {action_label} ({action_entity})"
        ),
        plan=plan,
    )


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def compile_timed_alternation(
    user_text: str,
    *,
    resolve_entity: EntityResolver,
) -> CompiledAutomation | None:
    folded = _fold(user_text)
    folded = re.sub(r"\bla\s+pagar(?:as|a)?\b", "la apagaras", folded)
    resolved = resolve_entity(folded)
    if resolved is None or "enc" not in folded or "apag" not in folded:
        return None
    delay_match = re.search(
        r"\bdentro de\s+(\d+(?:[,.]\d+)?)\s*(?:s|seg|segs|segundos?)\b",
        folded,
    )
    interval_match = re.search(r"\bcada\s+(\d+(?:[,.]\d+)?)\s*segundos?\b", folded)
    repeat_match = re.search(r"\b(\d+)\s+veces\b", folded)
    if not delay_match or not interval_match or not repeat_match:
        return None

    delay_seconds = max(1.0, min(float(delay_match.group(1).replace(",", ".")), 86400.0))
    interval_seconds = max(0.1, min(float(interval_match.group(1).replace(",", ".")), 60.0))
    repeat_count = max(1, min(int(repeat_match.group(1)), 20))
    entity_id, label = resolved
    domain = entity_id.split(".", 1)[0]
    steps: list[dict] = [
        {
            "type": "service",
            "domain": domain,
            "service": "turn_on",
            "target": {"entity_id": entity_id},
            "expected_state": "on",
        }
    ]
    for _ in range(repeat_count):
        steps.extend(
            [
                {"type": "delay", "seconds": interval_seconds, "from_previous_action_start": True},
                {
                    "type": "service",
                    "domain": domain,
                    "service": "turn_off",
                    "target": {"entity_id": entity_id},
                    "expected_state": "off",
                },
                {"type": "delay", "seconds": interval_seconds, "from_previous_action_start": True},
                {
                    "type": "service",
                    "domain": domain,
                    "service": "turn_on",
                    "target": {"entity_id": entity_id},
                    "expected_state": "on",
                },
            ]
        )
    plan = validate_plan(
        {
            "version": 1,
            "name": f"Secuencia de {label}",
            "conditions": [],
            "steps": steps,
        }
    )
    return CompiledAutomation(
        delay_seconds=delay_seconds,
        title=f"Secuencia de {label} cada {interval_seconds:g} segundos",
        summary=(
            f"dentro de {delay_seconds:g} segundos encendera {label} y luego repetira "
            f"apagar/encender cada {interval_seconds:g} segundos, {repeat_count} veces"
        ),
        plan=plan,
    )
