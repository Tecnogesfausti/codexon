from __future__ import annotations

import json

from automation.schema import validate_plan


def decode_legacy_plan(instruction: str) -> dict | None:
    text = instruction.strip()
    switch_prefix = "DETERMINISTIC_SWITCH_SEQUENCE "
    if text.startswith(switch_prefix):
        payload = json.loads(text[len(switch_prefix) :])
        entity_id = str(payload["entity_id"])
        label = str(payload.get("label") or entity_id)
        interval = max(0.1, min(float(payload["interval_seconds"]), 60.0))
        repeats = max(1, min(int(payload["repeat_count"]), 20))
        repeat_services = [str(item) for item in payload.get("repeat_services") or []]
        if repeat_services != ["turn_off", "turn_on"]:
            raise ValueError("Secuencia heredada no soportada")
        domain = entity_id.split(".", 1)[0]
        steps: list[dict] = [
            {
                "type": "service",
                "domain": domain,
                "service": str(payload.get("initial_service") or "turn_on"),
                "target": {"entity_id": entity_id},
                "expected_state": "on",
            }
        ]
        for _ in range(repeats):
            for service in repeat_services:
                steps.extend(
                    [
                        {"type": "delay", "seconds": interval, "from_previous_action_start": True},
                        {
                            "type": "service",
                            "domain": domain,
                            "service": service,
                            "target": {"entity_id": entity_id},
                            "expected_state": "on" if service == "turn_on" else "off",
                        },
                    ]
                )
        return validate_plan(
            {"version": 1, "name": f"Secuencia heredada de {label}", "conditions": [], "steps": steps}
        )

    power_prefix = "DETERMINISTIC_POWER_THRESHOLD_ACTION "
    if text.startswith(power_prefix):
        payload = json.loads(text[len(power_prefix) :])
        operator = {"<": "lt", "<=": "lte", ">": "gt", ">=": "gte", "==": "eq", "!=": "ne"}.get(
            str(payload.get("operator") or "<"), "lt"
        )
        action_service = str(payload["action_service"])
        return validate_plan(
            {
                "version": 1,
                "name": f"Umbral heredado {payload.get('subject') or payload['sensor_entity_id']}",
                "conditions": [
                    {
                        "entity_id": str(payload["sensor_entity_id"]),
                        "operator": operator,
                        "value": float(payload["threshold"]),
                    }
                ],
                "condition_policy": {
                    "on_false": "reschedule",
                    "delay_seconds": int(payload.get("delay_minutes", 1) or 1) * 60,
                },
                "steps": [
                    {
                        "type": "service",
                        "domain": str(payload["action_domain"]),
                        "service": action_service,
                        "target": {"entity_id": str(payload["action_entity_id"])},
                        "expected_state": "on" if action_service == "turn_on" else "off",
                    }
                ],
            }
        )
    return None
