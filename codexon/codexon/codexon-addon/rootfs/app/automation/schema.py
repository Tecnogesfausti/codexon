from __future__ import annotations

import copy
import datetime as dt
import json
import re
from typing import Any


AUTOMATION_PLAN_PREFIX = "AUTOMATION_PLAN_V1 "
ALLOWED_OPERATORS = {"eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in"}
ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
SERVICE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
BUILTIN_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
ALLOWED_BUILTIN_STEPS = {
    "ha_send_mobile_alert",
    "say_time",
    "terminal_message",
    "wait_seconds",
}


def _number(value: Any, *, field: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} debe ser numerico") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field} debe estar entre {minimum:g} y {maximum:g}")
    return parsed


def _entity_id(value: Any, *, field: str) -> str:
    entity_id = str(value or "").strip()
    if not ENTITY_ID_RE.fullmatch(entity_id):
        raise ValueError(f"{field} no es un entity_id valido: {entity_id}")
    return entity_id


def _iso_datetime(value: Any, *, field: str) -> str:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} debe ser ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} debe incluir zona horaria")
    return parsed.astimezone(dt.UTC).isoformat(timespec="microseconds")


def validate_plan(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("automation_plan debe ser un objeto JSON")
    plan = copy.deepcopy(payload)
    version = int(plan.get("version", 1) or 1)
    if version != 1:
        raise ValueError(f"Version de automation_plan no soportada: {version}")
    plan["version"] = version
    plan["name"] = str(plan.get("name") or "Automatizacion").strip()[:160]
    if plan.get("expires_at"):
        plan["expires_at"] = _iso_datetime(plan.get("expires_at"), field="expires_at")
    else:
        plan.pop("expires_at", None)

    raw_conditions = plan.get("conditions") or []
    if not isinstance(raw_conditions, list) or len(raw_conditions) > 20:
        raise ValueError("conditions debe ser una lista de hasta 20 elementos")
    conditions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_conditions):
        if not isinstance(raw, dict):
            raise ValueError(f"conditions[{index}] debe ser un objeto")
        condition_type = str(raw.get("type") or "state").strip().lower()
        if condition_type not in {"state", "transition"}:
            raise ValueError(f"Tipo no soportado en conditions[{index}]: {condition_type}")
        operator = str(raw.get("operator") or ("gte" if condition_type == "transition" else "eq")).strip().lower()
        if operator not in ALLOWED_OPERATORS:
            raise ValueError(f"Operador no soportado en conditions[{index}]: {operator}")
        if "value" not in raw:
            raise ValueError(f"conditions[{index}].value es obligatorio")
        condition = {
            "type": condition_type,
            "entity_id": _entity_id(raw.get("entity_id"), field=f"conditions[{index}].entity_id"),
            "operator": operator,
            "value": raw["value"],
        }
        if condition_type == "transition":
            condition["from_state"] = str(raw.get("from_state", "off") or "off").strip().lower()
            condition["to_state"] = str(raw.get("to_state", "on") or "on").strip().lower()
            condition["after"] = _iso_datetime(raw.get("after"), field=f"conditions[{index}].after")
            condition["settle_seconds"] = _number(
                raw.get("settle_seconds", 5),
                field=f"conditions[{index}].settle_seconds",
                minimum=0,
                maximum=60,
            )
            conditions.append(condition)
            continue
        attribute = str(raw.get("attribute") or "").strip()
        if attribute:
            condition["attribute"] = attribute
        conditions.append(condition)
    plan["conditions"] = conditions

    policy = plan.get("condition_policy") or {"on_false": "fail"}
    if not isinstance(policy, dict):
        raise ValueError("condition_policy debe ser un objeto")
    on_false = str(policy.get("on_false") or "fail").strip().lower()
    if on_false not in {"fail", "reschedule", "complete"}:
        raise ValueError("condition_policy.on_false debe ser fail, reschedule o complete")
    normalized_policy: dict[str, Any] = {"on_false": on_false}
    if on_false == "reschedule":
        normalized_policy["delay_seconds"] = _number(
            policy.get("delay_seconds", 60),
            field="condition_policy.delay_seconds",
            minimum=1,
            maximum=86400,
        )
    plan["condition_policy"] = normalized_policy

    raw_completion = plan.get("completion_notification")
    if raw_completion:
        if not isinstance(raw_completion, dict):
            raise ValueError("completion_notification debe ser un objeto")
        plan["completion_notification"] = {
            "enabled": bool(raw_completion.get("enabled", True)),
            "title": str(raw_completion.get("title") or "Codexon").strip()[:120],
            "message": str(
                raw_completion.get("message") or f"Tarea completada: {plan['name']}"
            ).strip()[:1000],
            "notify": str(
                raw_completion.get("notify") or "notify/mobile_app_sm_a566b"
            ).strip(),
            "volume": int(
                _number(
                    raw_completion.get("volume", 100),
                    field="completion_notification.volume",
                    minimum=0,
                    maximum=100,
                )
            ),
            "media_stream": str(
                raw_completion.get("media_stream") or "alarm_stream"
            ).strip(),
            "speak": bool(raw_completion.get("speak", True)),
            "critical": bool(raw_completion.get("critical", True)),
        }
    else:
        plan.pop("completion_notification", None)

    raw_steps = plan.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 100:
        raise ValueError("steps debe contener entre 1 y 100 elementos")
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"steps[{index}] debe ser un objeto")
        step_type = str(raw.get("type") or "").strip().lower()
        if step_type == "delay":
            steps.append(
                {
                    "type": "delay",
                    "seconds": _number(raw.get("seconds"), field=f"steps[{index}].seconds", minimum=0.1, maximum=60),
                    "from_previous_action_start": bool(raw.get("from_previous_action_start", False)),
                }
            )
            continue
        if step_type == "builtin":
            name = str(raw.get("name") or "").strip()
            if not BUILTIN_NAME_RE.fullmatch(name) or name not in ALLOWED_BUILTIN_STEPS:
                raise ValueError(f"Builtin no soportado en steps[{index}]: {name}")
            args = raw.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError(f"steps[{index}].args debe ser un objeto")
            steps.append(
                {
                    "type": "builtin",
                    "name": name,
                    "args": copy.deepcopy(args),
                }
            )
            continue
        if step_type != "service":
            raise ValueError(f"Tipo no soportado en steps[{index}]: {step_type}")
        domain = str(raw.get("domain") or "").strip()
        service = str(raw.get("service") or "").strip()
        if not SERVICE_NAME_RE.fullmatch(domain) or not SERVICE_NAME_RE.fullmatch(service):
            raise ValueError(f"Dominio o servicio invalido en steps[{index}]")
        target = raw.get("target") or {}
        if not isinstance(target, dict):
            raise ValueError(f"steps[{index}].target debe ser un objeto")
        normalized_target = copy.deepcopy(target)
        if "entity_id" in normalized_target:
            entity_value = normalized_target["entity_id"]
            if isinstance(entity_value, list):
                normalized_target["entity_id"] = [
                    _entity_id(item, field=f"steps[{index}].target.entity_id") for item in entity_value
                ]
            else:
                normalized_target["entity_id"] = _entity_id(
                    entity_value, field=f"steps[{index}].target.entity_id"
                )
        service_data = raw.get("service_data") or {}
        if not isinstance(service_data, dict):
            raise ValueError(f"steps[{index}].service_data debe ser un objeto")
        step: dict[str, Any] = {
            "type": "service",
            "domain": domain,
            "service": service,
            "target": normalized_target,
            "service_data": copy.deepcopy(service_data),
            "confirm": bool(raw.get("confirm", True)),
        }
        if raw.get("expected_state") is not None:
            step["expected_state"] = str(raw["expected_state"])
            verify_entity_id = raw.get("verify_entity_id") or normalized_target.get("entity_id")
            if isinstance(verify_entity_id, list) or not verify_entity_id:
                raise ValueError(f"steps[{index}].verify_entity_id es obligatorio para verificar varios destinos")
            target_entity_id = normalized_target.get("entity_id")
            if (
                isinstance(target_entity_id, str)
                and str(step["expected_state"]).strip().lower() in {"on", "off"}
                and str(verify_entity_id).split(".", 1)[0] in {"sensor", "input_number", "number"}
            ):
                verify_entity_id = target_entity_id
            step["verify_entity_id"] = _entity_id(
                verify_entity_id, field=f"steps[{index}].verify_entity_id"
            )
        steps.append(step)
    plan["steps"] = steps
    return plan


def encode_plan(payload: dict[str, Any]) -> str:
    return AUTOMATION_PLAN_PREFIX + json.dumps(validate_plan(payload), ensure_ascii=False, separators=(",", ":"))


def decode_plan(instruction: str) -> dict[str, Any] | None:
    text = instruction.strip()
    if not text.startswith(AUTOMATION_PLAN_PREFIX):
        return None
    return validate_plan(json.loads(text[len(AUTOMATION_PLAN_PREFIX) :]))


def automation_plan_json_schema() -> dict[str, Any]:
    condition = {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["state"]},
                    "entity_id": {"type": "string"},
                    "attribute": {"type": "string"},
                    "operator": {"type": "string", "enum": sorted(ALLOWED_OPERATORS)},
                    "value": {},
                },
                "required": ["type", "entity_id", "operator", "value"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["transition"]},
                    "entity_id": {"type": "string"},
                    "from_state": {"type": "string"},
                    "to_state": {"type": "string"},
                    "after": {"type": "string", "description": "Cursor ISO 8601 persistente."},
                    "settle_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 60,
                        "description": "Margen para que Recorder consolide los estados; por defecto 5 segundos.",
                    },
                    "operator": {"type": "string", "enum": sorted(ALLOWED_OPERATORS)},
                    "value": {},
                },
                "required": ["type", "entity_id", "from_state", "to_state", "after", "operator", "value"],
                "additionalProperties": False,
            },
        ]
    }
    step = {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["delay"]},
                    "seconds": {"type": "number", "minimum": 0.1, "maximum": 60},
                    "from_previous_action_start": {"type": "boolean"},
                },
                "required": ["type", "seconds"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["service"]},
                    "domain": {"type": "string"},
                    "service": {"type": "string"},
                    "target": {"type": "object"},
                    "service_data": {"type": "object"},
                    "confirm": {"type": "boolean"},
                    "expected_state": {"type": "string"},
                    "verify_entity_id": {"type": "string"},
                },
                "required": ["type", "domain", "service", "target"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["builtin"]},
                    "name": {"type": "string", "enum": sorted(ALLOWED_BUILTIN_STEPS)},
                    "args": {"type": "object"},
                },
                "required": ["type", "name"],
                "additionalProperties": False,
            },
        ]
    }
    return {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "enum": [1]},
            "name": {"type": "string"},
            "expires_at": {
                "type": "string",
                "description": "Fecha/hora ISO 8601 opcional. Si ya ha pasado, el plan se marca completado sin ejecutar pasos.",
            },
            "conditions": {"type": "array", "items": condition, "maxItems": 20},
            "condition_policy": {
                "type": "object",
                "properties": {
                    "on_false": {"type": "string", "enum": ["fail", "reschedule", "complete"]},
                    "delay_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
                },
                "required": ["on_false"],
                "additionalProperties": False,
            },
            "completion_notification": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean"},
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "notify": {"type": "string"},
                    "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                    "media_stream": {"type": "string"},
                    "speak": {"type": "boolean"},
                    "critical": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            "steps": {"type": "array", "items": step, "minItems": 1, "maxItems": 100},
        },
        "required": ["version", "name", "conditions", "steps"],
        "additionalProperties": False,
    }
