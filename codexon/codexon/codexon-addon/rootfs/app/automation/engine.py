from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from automation.schema import validate_plan


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class AutomationOutcome:
    status: str
    message: str
    reschedule_seconds: float | None = None
    updated_plan: dict[str, Any] | None = None


def _as_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("un booleano no es un valor numerico")
    return float(value)


def compare_values(actual: Any, operator: str, expected: Any) -> bool:
    if operator in {"lt", "lte", "gt", "gte"}:
        left = _as_number(actual)
        right = _as_number(expected)
        return {
            "lt": left < right,
            "lte": left <= right,
            "gt": left > right,
            "gte": left >= right,
        }[operator]
    if operator in {"in", "not_in"}:
        if not isinstance(expected, (list, tuple, set)):
            raise ValueError(f"El operador {operator} requiere una lista como valor esperado")
        contained = actual in expected or str(actual) in {str(item) for item in expected}
        return contained if operator == "in" else not contained
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        equal = _as_number(actual) == float(expected)
    elif isinstance(expected, bool):
        normalized = str(actual).strip().lower()
        equal = normalized in ({"on", "true", "1", "yes"} if expected else {"off", "false", "0", "no"})
    else:
        equal = str(actual).strip().lower() == str(expected).strip().lower()
    return equal if operator == "eq" else not equal


def _transition_count_after_cursor(data: dict[str, Any], cursor: str) -> int:
    cursor_at = dt.datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    count = 0
    for row in data.get("results") or []:
        for value in row.get("transition_times") or []:
            if not value:
                continue
            changed_at = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if changed_at > cursor_at:
                count += 1
    return count


class AutomationExecutor:
    def __init__(self, call_tool: ToolCaller) -> None:
        self.call_tool = call_tool

    async def _execute_builtin_step(self, step: dict[str, Any]) -> str:
        name = step["name"]
        args = dict(step.get("args") or {})
        if name == "say_time":
            timezone = str(args.get("timezone") or "Europe/Madrid")
            try:
                from zoneinfo import ZoneInfo

                now = dt.datetime.now(ZoneInfo(timezone))
            except Exception:
                now = dt.datetime.now(dt.UTC)
            message = str(args.get("message") or "Son las {time}").format(
                time=now.strftime("%H:%M:%S"),
                date=now.strftime("%Y-%m-%d"),
            )
            print(message, flush=True)
            return message
        if name == "terminal_message":
            message = str(args.get("message") or "").strip()
            if message:
                print(message, flush=True)
            return message
        if name == "wait_seconds":
            await self.call_tool("__builtin__:wait_seconds", {"seconds": args.get("seconds", 1)})
            return f"Espera completada: {args.get('seconds', 1)}s"
        if name == "ha_send_mobile_alert":
            await self.call_tool("__builtin__:ha_send_mobile_alert", args)
            return "Aviso movil enviado."
        raise ValueError(f"Builtin no soportado: {name}")

    async def execute(self, payload: dict[str, Any]) -> AutomationOutcome:
        plan = validate_plan(payload)
        if plan.get("expires_at"):
            expires_at = dt.datetime.fromisoformat(str(plan["expires_at"]).replace("Z", "+00:00"))
            if dt.datetime.now(dt.UTC) > expires_at:
                return AutomationOutcome(
                    "completed",
                    f"Plan '{plan['name']}' omitido: vencio en {plan['expires_at']}.",
                    updated_plan=plan,
                )
        for condition in plan["conditions"]:
            evaluated_until: str | None = None
            if condition["type"] == "transition":
                cursor_at = dt.datetime.fromisoformat(condition["after"].replace("Z", "+00:00"))
                settled_until = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=float(condition["settle_seconds"]))
                if settled_until <= cursor_at:
                    policy = plan["condition_policy"]
                    detail = "Ventana historica pendiente de consolidacion en Recorder."
                    if policy["on_false"] == "reschedule":
                        return AutomationOutcome(
                            "reschedule", detail, float(policy["delay_seconds"]), plan
                        )
                    if policy["on_false"] == "complete":
                        return AutomationOutcome("completed", detail, updated_plan=plan)
                    raise RuntimeError(detail)
                evaluated_until = settled_until.isoformat(timespec="microseconds")
                transition_data = json.loads(
                    await self.call_tool(
                        "__builtin__:ha_count_state_transitions",
                        {
                            "start_time": condition["after"],
                            "end_time": evaluated_until,
                            "entity_id": condition["entity_id"],
                            "from_state": condition["from_state"],
                            "to_state": condition["to_state"],
                        },
                    )
                )
                actual: Any = _transition_count_after_cursor(transition_data, condition["after"])
            else:
                state = json.loads(
                    await self.call_tool("__builtin__:ha_get_state", {"entity_id": condition["entity_id"]})
                )
                actual = state.get("state")
                if condition.get("attribute"):
                    actual = state.get("attributes") or {}
                    for part in str(condition["attribute"]).split("."):
                        actual = actual.get(part) if isinstance(actual, dict) else None
            matched = compare_values(actual, condition["operator"], condition["value"])
            # Recorder is eventually consistent. Advancing an empty window can
            # permanently skip a short transition that appears in history later.
            if evaluated_until and matched:
                condition["after"] = evaluated_until
            if matched:
                continue
            detail = (
                f"Condicion no cumplida: {condition['entity_id']} valor {actual!r} "
                f"{condition['operator']} {condition['value']!r}."
            )
            policy = plan["condition_policy"]
            if policy["on_false"] == "reschedule":
                return AutomationOutcome(
                    "reschedule",
                    detail,
                    float(policy["delay_seconds"]),
                    plan,
                )
            if policy["on_false"] == "complete":
                return AutomationOutcome("completed", detail, updated_plan=plan)
            raise RuntimeError(detail)

        action_started: float | None = None
        service_count = 0
        builtin_count = 0
        builtin_messages: list[str] = []
        for index, step in enumerate(plan["steps"]):
            if step["type"] == "delay":
                seconds = float(step["seconds"])
                if step.get("from_previous_action_start") and action_started is not None:
                    seconds = max(0.0, seconds - (time.monotonic() - action_started))
                if seconds > 0:
                    await self.call_tool("__builtin__:wait_seconds", {"seconds": seconds})
                continue
            if step["type"] == "builtin":
                action_started = time.monotonic()
                message = await self._execute_builtin_step(step)
                builtin_count += 1
                if message:
                    builtin_messages.append(message)
                continue

            action_started = time.monotonic()
            await self.call_tool(
                "__builtin__:ha_call_service",
                {
                    "domain": step["domain"],
                    "service": step["service"],
                    "service_data": step["service_data"],
                    "target": step["target"],
                    "confirm": step["confirm"],
                },
            )
            service_count += 1
            if "expected_state" not in step:
                continue
            verify = json.loads(
                await self.call_tool(
                    "__builtin__:ha_get_state", {"entity_id": step["verify_entity_id"]}
                )
            )
            observed = str(verify.get("state"))
            if observed != step["expected_state"]:
                raise RuntimeError(
                    f"Paso {index + 1}: {step['verify_entity_id']} debia quedar "
                    f"{step['expected_state']}, estado observado {observed}"
                )

        notification_detail = ""
        completion_notification = plan.get("completion_notification")
        if completion_notification and completion_notification.get("enabled", True):
            try:
                notification_args = dict(completion_notification)
                notification_args.pop("enabled", None)
                await self.call_tool(
                    "__builtin__:ha_send_mobile_alert",
                    notification_args,
                )
                notification_detail = " Aviso movil de finalizacion enviado."
            except Exception as exc:  # La accion ya termino; no debe repetirse por fallar el aviso.
                notification_detail = f" La accion termino, pero fallo el aviso movil: {exc}"

        if builtin_count:
            execution_detail = (
                f"{service_count} servicios y {builtin_count} acciones internas ejecutadas."
                f"{(' ' + ' | '.join(builtin_messages[-3:])) if builtin_messages else ''}"
            )
        else:
            execution_detail = f"{service_count} servicios ejecutados."
        return AutomationOutcome(
            "completed",
            f"Plan '{plan['name']}' completado: {execution_detail}{notification_detail}",
            updated_plan=plan,
        )
