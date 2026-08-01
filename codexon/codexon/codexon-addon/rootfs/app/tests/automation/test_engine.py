from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from automation import AutomationExecutor, compare_values, decode_plan, encode_plan


class ValueComparisonTest(unittest.TestCase):
    def test_numeric_and_binary_values_share_one_comparator(self) -> None:
        self.assertTrue(compare_values("12.5", "gt", 10))
        self.assertTrue(compare_values("on", "eq", True))
        self.assertTrue(compare_values("off", "ne", "on"))
        self.assertTrue(compare_values("open", "in", ["open", "opening"]))

    def test_plan_round_trip_is_versioned(self) -> None:
        plan = {
            "version": 1,
            "name": "Prueba",
            "conditions": [],
            "steps": [
                {
                    "type": "service",
                    "domain": "switch",
                    "service": "turn_on",
                    "target": {"entity_id": "switch.test"},
                    "expected_state": "on",
                }
            ],
        }
        self.assertEqual(decode_plan(encode_plan(plan))["name"], "Prueba")

    def test_plan_round_trip_preserves_completion_notification(self) -> None:
        plan = {
            "version": 1,
            "name": "Cerrar valvula",
            "conditions": [],
            "steps": [
                {
                    "type": "service",
                    "domain": "switch",
                    "service": "turn_off",
                    "target": {"entity_id": "switch.valvula"},
                }
            ],
            "completion_notification": {
                "message": "Valvula cerrada",
            },
        }
        decoded = decode_plan(encode_plan(plan))
        self.assertEqual(decoded["completion_notification"]["message"], "Valvula cerrada")
        self.assertTrue(decoded["completion_notification"]["enabled"])
        self.assertTrue(decoded["completion_notification"]["speak"])
        self.assertTrue(decoded["completion_notification"]["critical"])

    def test_on_off_expected_state_does_not_verify_numeric_power_sensor(self) -> None:
        plan = {
            "version": 1,
            "name": "AC",
            "conditions": [],
            "steps": [
                {
                    "type": "service",
                    "domain": "input_boolean",
                    "service": "turn_on",
                    "target": {"entity_id": "input_boolean.brokton_ac_dp1_switch"},
                    "expected_state": "on",
                    "verify_entity_id": "sensor.powcasa_enchufes_powcasa_enchufes_power",
                }
            ],
        }

        decoded = decode_plan(encode_plan(plan))

        self.assertEqual(
            decoded["steps"][0]["verify_entity_id"],
            "input_boolean.brokton_ac_dp1_switch",
        )

    def test_plan_round_trip_preserves_builtin_steps(self) -> None:
        plan = {
            "version": 1,
            "name": "Decir hora",
            "expires_at": "2030-01-01T00:00:00+00:00",
            "conditions": [],
            "steps": [
                {
                    "type": "builtin",
                    "name": "say_time",
                    "args": {"timezone": "Europe/Madrid", "message": "Hora: {time}"},
                }
            ],
        }

        decoded = decode_plan(encode_plan(plan))

        self.assertEqual(decoded["steps"][0]["type"], "builtin")
        self.assertEqual(decoded["steps"][0]["name"], "say_time")
        self.assertEqual(decoded["expires_at"], "2030-01-01T00:00:00.000000+00:00")


class AutomationExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_completion_notification_is_not_sent(self) -> None:
        calls: list[str] = []

        async def call_tool(name: str, args: dict) -> str:
            calls.append(name)
            if name == "__builtin__:ha_call_service":
                return json.dumps({"success": True})
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Accion sin aviso",
                "conditions": [],
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_off",
                        "target": {"entity_id": "switch.valvula"},
                    }
                ],
                "completion_notification": {"enabled": False},
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertNotIn("__builtin__:ha_send_mobile_alert", calls)

    async def test_completion_notification_runs_after_verified_action(self) -> None:
        calls: list[tuple[str, dict]] = []
        state = "on"

        async def call_tool(name: str, args: dict) -> str:
            nonlocal state
            calls.append((name, args))
            if name == "__builtin__:ha_call_service":
                state = "off"
                return json.dumps({"success": True})
            if name == "__builtin__:ha_get_state":
                return json.dumps({"state": state})
            if name == "__builtin__:ha_send_mobile_alert":
                return json.dumps({"called": True})
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Cerrar agua",
                "conditions": [],
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_off",
                        "target": {"entity_id": "switch.valvula"},
                        "expected_state": "off",
                    }
                ],
                "completion_notification": {"message": "Agua cerrada"},
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(calls[-1][0], "__builtin__:ha_send_mobile_alert")
        self.assertEqual(calls[-1][1]["message"], "Agua cerrada")

    async def test_notification_failure_does_not_repeat_completed_action(self) -> None:
        service_calls = 0

        async def call_tool(name: str, args: dict) -> str:
            nonlocal service_calls
            if name == "__builtin__:ha_call_service":
                service_calls += 1
                return json.dumps({"success": True})
            if name == "__builtin__:ha_send_mobile_alert":
                raise RuntimeError("movil desconectado")
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Sirena",
                "conditions": [],
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_on",
                        "target": {"entity_id": "switch.sirena"},
                    }
                ],
                "completion_notification": {"message": "Sirena activada"},
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(service_calls, 1)
        self.assertIn("fallo el aviso movil", outcome.message)

    async def test_builtin_wait_uses_same_executor_path(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def call_tool(name: str, args: dict) -> str:
            calls.append((name, args))
            if name == "__builtin__:wait_seconds":
                return json.dumps({"waited_seconds": args["seconds"]})
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Espera interna",
                "conditions": [],
                "steps": [
                    {"type": "builtin", "name": "wait_seconds", "args": {"seconds": 2}},
                ],
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(calls, [("__builtin__:wait_seconds", {"seconds": 2})])
        self.assertIn("1 acciones internas", outcome.message)

    async def test_builtin_say_time_does_not_call_llm_or_ha(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def call_tool(name: str, args: dict) -> str:
            calls.append((name, args))
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Hora",
                "conditions": [],
                "steps": [
                    {
                        "type": "builtin",
                        "name": "say_time",
                        "args": {"timezone": "Europe/Madrid", "message": "Son las {time}"},
                    }
                ],
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(calls, [])
        self.assertIn("acciones internas", outcome.message)

    async def test_expired_plan_skips_steps(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def call_tool(name: str, args: dict) -> str:
            calls.append((name, args))
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Caducado",
                "expires_at": "2000-01-01T00:00:00+00:00",
                "conditions": [],
                "steps": [
                    {"type": "builtin", "name": "terminal_message", "args": {"message": "no debe salir"}},
                ],
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(calls, [])
        self.assertIn("omitido", outcome.message)

    async def test_brief_transition_is_detected_after_sensor_returns_off(self) -> None:
        calls: list[tuple[str, dict]] = []
        switch_state = "off"

        async def call_tool(name: str, args: dict) -> str:
            nonlocal switch_state
            calls.append((name, args))
            if name == "__builtin__:ha_count_state_transitions":
                return json.dumps(
                    {
                        "total_transitions": 1,
                        "results": [{"transition_times": ["2025-01-01T00:00:00+00:00"]}],
                    }
                )
            if name == "__builtin__:ha_call_service":
                switch_state = "on"
                return json.dumps({"success": True})
            if name == "__builtin__:ha_get_state":
                return json.dumps({"state": switch_state, "attributes": {}})
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Pulso timbre",
                "conditions": [
                    {
                        "type": "transition",
                        "entity_id": "binary_sensor.timbre",
                        "from_state": "off",
                        "to_state": "on",
                        "after": "2020-01-01T10:00:00+00:00",
                        "operator": "gte",
                        "value": 1,
                    }
                ],
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_on",
                        "target": {"entity_id": "switch.luz"},
                        "expected_state": "on",
                    }
                ],
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(switch_state, "on")
        transition_args = next(args for name, args in calls if name == "__builtin__:ha_count_state_transitions")
        self.assertEqual(transition_args["from_state"], "off")
        self.assertEqual(transition_args["to_state"], "on")
        self.assertIn("end_time", transition_args)
        settled_end = datetime.fromisoformat(transition_args["end_time"])
        self.assertGreaterEqual((datetime.now(UTC) - settled_end).total_seconds(), 4.5)
        self.assertGreater(outcome.updated_plan["conditions"][0]["after"], "2020-01-01T10:00:00+00:00")

    async def test_transition_cursor_waits_when_window_has_no_events(self) -> None:
        async def call_tool(name: str, args: dict) -> str:
            if name == "__builtin__:ha_count_state_transitions":
                return json.dumps(
                    {
                        "total_transitions": 1,
                        "results": [{"transition_times": ["2020-01-01T10:00:00+00:00"]}],
                    }
                )
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Espera pulso",
                "conditions": [
                    {
                        "type": "transition",
                        "entity_id": "binary_sensor.puerta",
                        "from_state": "off",
                        "to_state": "on",
                        "after": "2020-01-01T10:00:00+00:00",
                        "operator": "gte",
                        "value": 1,
                    }
                ],
                "condition_policy": {"on_false": "reschedule", "delay_seconds": 5},
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_on",
                        "target": {"entity_id": "switch.luz"},
                    }
                ],
            }
        )

        self.assertEqual(outcome.status, "reschedule")
        self.assertEqual(outcome.reschedule_seconds, 5)
        cursor = datetime.fromisoformat(outcome.updated_plan["conditions"][0]["after"])
        self.assertEqual(cursor, datetime.fromisoformat("2020-01-01T10:00:00+00:00"))

    async def test_binary_sensor_condition_executes_generic_service_step(self) -> None:
        states = {
            "binary_sensor.puerta": "on",
            "switch.luz": "off",
        }
        calls: list[tuple[str, dict]] = []

        async def call_tool(name: str, args: dict) -> str:
            calls.append((name, args))
            if name == "__builtin__:ha_get_state":
                return json.dumps({"state": states[args["entity_id"]], "attributes": {}})
            if name == "__builtin__:ha_call_service":
                entity_id = args["target"]["entity_id"]
                states[entity_id] = "on" if args["service"] == "turn_on" else "off"
                return json.dumps({"success": True})
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Puerta activa enciende luz",
                "conditions": [
                    {"entity_id": "binary_sensor.puerta", "operator": "eq", "value": "on"}
                ],
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_on",
                        "target": {"entity_id": "switch.luz"},
                        "expected_state": "on",
                    }
                ],
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(states["switch.luz"], "on")
        self.assertEqual(len([name for name, _ in calls if name == "__builtin__:ha_call_service"]), 1)

    async def test_numeric_condition_reschedules_without_running_actions(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def call_tool(name: str, args: dict) -> str:
            calls.append((name, args))
            if name == "__builtin__:ha_get_state":
                return json.dumps({"state": "650.2", "attributes": {}})
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Potencia baja",
                "conditions": [
                    {"entity_id": "sensor.potencia", "operator": "lt", "value": 500}
                ],
                "condition_policy": {"on_false": "reschedule", "delay_seconds": 30},
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_on",
                        "target": {"entity_id": "switch.luz"},
                    }
                ],
            }
        )

        self.assertEqual(outcome.status, "reschedule")
        self.assertEqual(outcome.reschedule_seconds, 30)
        self.assertFalse(any(name == "__builtin__:ha_call_service" for name, _ in calls))

    async def test_numeric_attribute_can_drive_same_engine(self) -> None:
        states = {"switch.ventilador": "off"}

        async def call_tool(name: str, args: dict) -> str:
            if name == "__builtin__:ha_get_state":
                if args["entity_id"] == "sensor.clima":
                    return json.dumps({"state": "ok", "attributes": {"metrics": {"humidity": 72}}})
                return json.dumps({"state": states[args["entity_id"]], "attributes": {}})
            if name == "__builtin__:ha_call_service":
                states[args["target"]["entity_id"]] = "on"
                return json.dumps({"success": True})
            raise AssertionError(name)

        outcome = await AutomationExecutor(call_tool).execute(
            {
                "version": 1,
                "name": "Humedad alta",
                "conditions": [
                    {
                        "entity_id": "sensor.clima",
                        "attribute": "metrics.humidity",
                        "operator": "gte",
                        "value": 70,
                    }
                ],
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_on",
                        "target": {"entity_id": "switch.ventilador"},
                        "expected_state": "on",
                    }
                ],
            }
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(states["switch.ventilador"], "on")


if __name__ == "__main__":
    unittest.main()
