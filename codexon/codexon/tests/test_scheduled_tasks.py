from __future__ import annotations

import json
import unittest
import datetime as dt
from unittest.mock import patch

from automation import decode_plan, encode_plan
from codexon import (
    CodexonAgent,
    completion_notification_requested,
    critical_action_requested,
    historical_active_device_measurement_intent,
    lexical_correction_suggestion,
    requested_ac_pvpc_valley_plan,
    requested_ac_until_price_drop_plan,
    requested_history_date,
    readonly_entity_inventory_intent,
    requested_weekday_date,
    scheduling_intent_hint,
    should_offer_critical_completion_alert,
    task_recovery_policy,
)
from site_profile import SiteProfile


TEST_SITE_PROFILE = SiteProfile(
    path=None,
    roles={
        "lighting.dining": {
            "entity_id": "switch.relestftcomedor_rele1",
            "label": "luz del comedor",
            "aliases": ["luz del comedor", "luz de comedor", "luz comedor"],
        },
        "lighting.kitchen": {
            "entity_id": "switch.nspanel_relay_2",
            "label": "luz de la cocina",
            "aliases": ["luz de la cocina", "luz de cocina", "luz cocina"],
        },
        "irrigation.flow_meter": {
            "entity_id": "sensor.controlh2oficina_acumulado_temporal_caudalimetro",
        },
        "irrigation.pass_valve": {
            "entity_id": "switch.controlh2oficina_relealmacen4",
        },
        "irrigation.alarms": {
            "entities": [
                "binary_sensor.riego_alarma_sin_agua",
                "binary_sensor.riego_conflicto_caudal",
            ],
        },
        "irrigation.primary.master": {
            "entity_id": "switch.riego_rele1",
        },
        "irrigation.primary.central": {
            "entity_id": "switch.riego_rele2",
            "label": "huerto central",
            "aliases": ["huerto central"],
            "kind": "zone",
            "default": True,
            "master_role": "irrigation.primary.master",
            "controller": {"programador": "riego", "zone": 2},
        },
        "irrigation.primary.office": {
            "entity_id": "switch.riego_rele7",
            "label": "riego oficina",
            "aliases": ["oficina"],
            "kind": "zone",
            "master_role": "irrigation.primary.master",
            "controller": {"programador": "riego", "zone": 7},
        },
        "irrigation.secondary.bonsai": {
            "entity_id": "switch.riego2_rele1",
            "label": "riego bonsais",
            "aliases": ["bonsais"],
            "kind": "zone",
            "controller": {"programador": "riego2", "zone": 1},
        },
    },
)
CodexonAgent.site_profile = TEST_SITE_PROFILE


class ScheduledTaskClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = CodexonAgent.__new__(CodexonAgent)
        self.agent.site_profile = TEST_SITE_PROFILE

        class FakeMemory:
            def __init__(self) -> None:
                self.tasks = []
                self.settings = {}

            def add_task(self, *, run_at: str, title: str, instruction: str, **kwargs) -> int:
                self.tasks.append({"run_at": run_at, "title": title, "instruction": instruction, **kwargs})
                return len(self.tasks)

            def get_setting(self, key: str, default: str = "") -> str:
                return self.settings.get(key, default)

            def set_setting(self, key: str, value: str | None) -> None:
                if value is None:
                    self.settings.pop(key, None)
                else:
                    self.settings[key] = value

            def search_entity_catalog(self, query: str, domain: str = "", limit: int = 8):
                if domain == "binary_sensor":
                    return [{"entity_id": "binary_sensor.automatismosf2_rob32_tecladoopen"}]
                return [{"entity_id": "sensor.nspanel_temperature"}]

        self.agent.memory = FakeMemory()

    def test_infinitive_homeassistant_action_is_not_a_simple_reminder(self) -> None:
        instruction = "Encender el switch 'Nspanel-978124 Relay 1' en el salón dentro de 2 minutos."

        self.assertTrue(self.agent.requires_execution_tool(instruction))
        self.assertIsNone(self.agent.simple_reminder_message(instruction))

    def test_plain_reminder_can_still_be_simple(self) -> None:
        self.assertEqual(
            self.agent.simple_reminder_message("recordarme revisar el correo"),
            "revisar el correo",
        )

    def test_entity_inventory_questions_are_read_only_queries(self) -> None:
        self.assertTrue(
            readonly_entity_inventory_intent("casa cuantos grifos puedo encender?")
        )
        self.assertTrue(
            readonly_entity_inventory_intent("lista todos los switches de riego")
        )
        self.assertFalse(
            readonly_entity_inventory_intent("enciende todos los grifos de la huerta")
        )

    def test_historical_device_consumption_requires_attributed_measurement(self) -> None:
        self.assertTrue(
            historical_active_device_measurement_intent(
                "casa cuantos litros ha gastado el grifo de los bonsais esta ultima semana?"
            )
        )
        self.assertTrue(
            historical_active_device_measurement_intent(
                "cuantos kWh consumio el aire acondicionado la semana pasada"
            )
        )
        self.assertFalse(
            historical_active_device_measurement_intent("riega los bonsais con 20 litros")
        )
        self.assertFalse(
            historical_active_device_measurement_intent(
                "cual es el entity_id del grifo de los bonsais"
            )
        )

    def test_critical_actions_offer_completion_alert_but_queries_do_not(self) -> None:
        self.assertEqual(
            should_offer_critical_completion_alert("cierra la valvula general de agua"),
            "agua",
        )
        self.assertEqual(
            should_offer_critical_completion_alert("activa la sirena del terreno"),
            "seguridad",
        )
        self.assertIsNone(
            should_offer_critical_completion_alert("cuanta agua consumi ayer")
        )
        self.assertIsNone(
            should_offer_critical_completion_alert(
                "cierra la valvula de agua y avisame cuando acabes"
            )
        )

    def test_completion_notification_phrases_are_recognized(self) -> None:
        self.assertTrue(completion_notification_requested("avisame cuando acabes"))
        self.assertTrue(completion_notification_requested("confirmame cuando este hecho"))
        self.assertFalse(completion_notification_requested("avisame si llueve"))

    def test_ac_until_price_drop_phrase_is_deterministic(self) -> None:
        text = (
            "pone el aire (sinonimo de AC A.C. Aire acondicionado) desde ahora hasta que "
            "baje el precio del kilowatio (sinonimo de kW/h kilovatio)."
        )

        self.assertTrue(requested_ac_until_price_drop_plan(text))
        self.assertFalse(requested_ac_pvpc_valley_plan(text))

    def test_lexical_correction_suggests_action_typo_and_unit(self) -> None:
        correction = lexical_correction_suggestion(
            "pone el aire hasta que baje el precio del kilowatio en kW/h"
        )

        self.assertIsNotNone(correction)
        self.assertTrue(correction.requires_confirmation)
        self.assertIn("pon el aire", correction.corrected)
        self.assertIn("kilovatio", correction.corrected)
        self.assertIn("kWh", correction.corrected)

    def test_put_air_is_critical_action(self) -> None:
        self.assertTrue(critical_action_requested("pon el aire hasta que baje el precio del kWh"))

    def test_ac_valley_plan_accepts_put_and_kilowatt_synonyms(self) -> None:
        self.assertTrue(
            requested_ac_pvpc_valley_plan(
                "pon el A.C. cuando baje el precio del kilovatio y mantenlo hasta que suba"
            )
        )

    def test_hides_mcp_action_tools_when_rest_tools_are_available(self) -> None:
        self.agent.has_homeassistant_rest = True
        self.assertTrue(self.agent.should_hide_mcp_tool("HassTurnOn"))
        self.assertFalse(self.agent.should_hide_mcp_tool("GetLiveContext"))

        self.agent.has_homeassistant_rest = False
        self.assertFalse(self.agent.should_hide_mcp_tool("HassTurnOn"))

    def test_numeric_condition_is_scheduled_without_llm(self) -> None:
        answer = self.agent.try_schedule_numeric_condition(
            "cuando la temperatura de nspanel sea mayord de 25 enciendes la luz de la cocina"
        )

        self.assertIsNotNone(answer)
        self.assertIn("tarea condicional generica #1", answer)
        self.assertEqual(len(self.agent.memory.tasks), 1)
        task = self.agent.memory.tasks[0]
        self.assertIn("temperatura de nspanel", task["title"])
        plan = decode_plan(task["instruction"])
        self.assertIsNotNone(plan)
        self.assertEqual(plan["conditions"][0], {
            "type": "state",
            "entity_id": "sensor.nspanel_temperature",
            "operator": "gt",
            "value": 25.0,
        })
        self.assertEqual(plan["steps"][0]["target"]["entity_id"], "switch.nspanel_relay_2")

    def test_water_liters_task_is_manual_recovery(self) -> None:
        self.assertEqual(
            task_recovery_policy(
                'DETERMINISTIC_WATER_LITERS {"switch_entity_id":"switch.riego_rele1","target_liters":10}'
            ),
            "manual",
        )

    def test_water_liters_bonsais_default_to_riego2_rele1(self) -> None:
        payload = self.agent.parse_water_liters_payload("regar 10 litros los bonsais")

        self.assertIsNotNone(payload)
        self.assertEqual(payload["switch_entity_id"], "switch.riego2_rele1")
        self.assertNotIn("master_switch_entity_id", payload)
        self.assertEqual(payload["appdaemon_manual"]["manual_button_entity_id"], "input_button.riego2_manual_zona1")

    def test_whatsapp_water_command_creates_deterministic_task(self) -> None:
        answer = self.agent.try_schedule_whatsapp_water_liters(
            "[Contexto del canal: mensaje entrante de WhatsApp de Martinson "
            "(34600123123@s.whatsapp.net). Tu respuesta final se enviará "
            "automáticamente a este mismo chat.]\n\n"
            "riega los bonsais con 20 litros"
        )

        self.assertIn("20 litros", answer)
        self.assertEqual(len(self.agent.memory.tasks), 1)
        task = self.agent.memory.tasks[0]
        self.assertEqual(task["priority"], 90)
        self.assertTrue(task["instruction"].startswith("DETERMINISTIC_WATER_LITERS "))
        payload = json.loads(
            task["instruction"].removeprefix("DETERMINISTIC_WATER_LITERS ")
        )
        self.assertEqual(payload["switch_entity_id"], "switch.riego2_rele1")
        self.assertEqual(payload["target_liters"], 20)
        self.assertEqual(
            payload["appdaemon_manual"]["target_liters_entity_id"],
            "input_number.riego2_litros_zona1",
        )
        self.assertTrue(payload["completion_alert"]["whatsapp"])
        self.assertFalse(payload["completion_alert"]["mobile_enabled"])

    def test_whatsapp_water_action_synonyms_create_same_task(self) -> None:
        phrases = (
            "regar los bonsais con 20 litros",
            "riega los bonsais con 20 litros",
            "echa 20 litros a los bonsais",
            "échale 20 litros a los bonsais",
            "dale 20 litros a los bonsais",
            "pon 20 litros a los bonsais",
            "ponle 20 litros a los bonsais",
            "tira 20 litros a los bonsais",
            "tírale 20 litros a los bonsais",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.agent.memory.tasks.clear()
                answer = self.agent.try_schedule_whatsapp_water_liters(
                    "[Contexto del canal: mensaje entrante de WhatsApp]\n\n"
                    + phrase
                )
                self.assertIsNotNone(answer)
                payload = json.loads(
                    self.agent.memory.tasks[0]["instruction"].removeprefix(
                        "DETERMINISTIC_WATER_LITERS "
                    )
                )
                self.assertEqual(payload["switch_entity_id"], "switch.riego2_rele1")
                self.assertEqual(payload["target_liters"], 20)

    def test_water_liters_riego1_zone_uses_master_relay(self) -> None:
        payload = self.agent.parse_water_liters_payload("regar 10 litros el huerto central")

        self.assertIsNotNone(payload)
        self.assertEqual(payload["switch_entity_id"], "switch.riego_rele2")
        self.assertEqual(payload["master_switch_entity_id"], "switch.riego_rele1")
        self.assertEqual(payload["appdaemon_manual"]["activation_mode_entity_id"], "input_select.riego_modo_activacion_zona2")
        self.assertEqual(payload["appdaemon_manual"]["manual_button_entity_id"], "input_button.riego_manual_zona2")

    def test_water_liters_ignores_master_when_zone_switch_is_mentioned(self) -> None:
        payload = self.agent.parse_water_liters_payload(
            "Iniciar riego de 50 litros en la oficina. Verificar que switch.riego_rele1 "
            "este apagado. Activar switch.riego_rele7 para zona oficina."
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["switch_entity_id"], "switch.riego_rele7")
        self.assertEqual(payload["master_switch_entity_id"], "switch.riego_rele1")
        self.assertEqual(payload["appdaemon_manual"]["manual_button_entity_id"], "input_button.riego_manual_zona7")

    def test_phrase_trigger_enqueues_generic_clock_batch(self) -> None:
        self.agent.register_phrase_trigger(
            {
                "phrase": "fuego",
                "title": "Prueba fuego reloj",
                "cancellation_key": "alto",
                "once": True,
                "priority": 80,
                "batch": {
                    "count": 10,
                    "spacing_seconds": 10,
                    "expires_after_seconds": 60,
                    "title_template": "Fuego reloj {index:02d}/{total}",
                    "plan": {
                        "version": 1,
                        "name": "Fuego reloj {index:02d}/{total}",
                        "conditions": [],
                        "steps": [
                            {
                                "type": "builtin",
                                "name": "say_time",
                                "args": {
                                    "timezone": "Europe/Madrid",
                                    "message": "Son las {time}",
                                },
                            }
                        ],
                    },
                },
            }
        )

        answer = self.agent.activate_phrase_trigger("fuego")

        self.assertIsNotNone(answer)
        self.assertEqual(len(self.agent.memory.tasks), 10)
        self.assertIn("alto", answer)
        run_ats = [dt.datetime.fromisoformat(task["run_at"]) for task in self.agent.memory.tasks]
        offsets = [int((run_at - run_ats[0]).total_seconds()) for run_at in run_ats]
        self.assertEqual(offsets, [0, 10, 20, 30, 40, 50, 60, 70, 80, 90])
        self.assertTrue(all(task["cancellation_key"] == "alto" for task in self.agent.memory.tasks))
        self.assertTrue(all(task["max_attempts"] == 1 for task in self.agent.memory.tasks))
        plan = decode_plan(self.agent.memory.tasks[0]["instruction"])
        self.assertEqual(plan["steps"][0]["type"], "builtin")
        self.assertEqual(plan["steps"][0]["name"], "say_time")
        self.assertIn("expires_at", plan)
        self.assertEqual(self.agent.load_phrase_triggers(), [])

    def test_message_batch_is_compiled_to_generic_plans(self) -> None:
        answer = self.agent.try_schedule_message_batch(
            'manda 10 tareas que digan "hola" cada 15 segundos hasta que diga basta'
        )

        self.assertIsNotNone(answer)
        self.assertEqual(len(self.agent.memory.tasks), 10)
        run_ats = [dt.datetime.fromisoformat(task["run_at"]) for task in self.agent.memory.tasks]
        offsets = [int((run_at - run_ats[0]).total_seconds()) for run_at in run_ats]
        self.assertEqual(offsets, [0, 15, 30, 45, 60, 75, 90, 105, 120, 135])
        self.assertTrue(all(task["cancellation_key"] == "basta" for task in self.agent.memory.tasks))
        plan = decode_plan(self.agent.memory.tasks[0]["instruction"])
        self.assertEqual(plan["steps"][0]["type"], "builtin")
        self.assertEqual(plan["steps"][0]["name"], "terminal_message")
        self.assertEqual(plan["steps"][0]["args"]["message"], "hola")

    def test_job_batch_half_minute_date_is_compiled_to_generic_plans(self) -> None:
        answer = self.agent.try_schedule_message_batch(
            "manda 10 trabajos cada medio minuto que saquen la fecha hasta que diga para"
        )

        self.assertIsNotNone(answer)
        self.assertEqual(len(self.agent.memory.tasks), 10)
        run_ats = [dt.datetime.fromisoformat(task["run_at"]) for task in self.agent.memory.tasks]
        offsets = [int((run_at - run_ats[0]).total_seconds()) for run_at in run_ats]
        self.assertEqual(offsets, [0, 30, 60, 90, 120, 150, 180, 210, 240, 270])
        self.assertTrue(all(task["cancellation_key"] == "para" for task in self.agent.memory.tasks))
        plan = decode_plan(self.agent.memory.tasks[0]["instruction"])
        self.assertEqual(plan["steps"][0]["type"], "builtin")
        self.assertEqual(plan["steps"][0]["name"], "say_time")
        self.assertEqual(plan["steps"][0]["args"]["message"], "Fecha: {date}")

    def test_timed_switch_alternation_is_scheduled_structurally(self) -> None:
        answer = self.agent.try_schedule_known_switch_sequence(
            "dentro de 20s enciende la luz de la cocina y luego la pagaras y enciendes cada 2 segundos 2 veces"
        )

        self.assertIsNotNone(answer)
        self.assertEqual(len(self.agent.memory.tasks), 1)
        instruction = self.agent.memory.tasks[0]["instruction"]
        plan = decode_plan(instruction)
        self.assertIsNotNone(plan)
        services = [step.get("service") for step in plan["steps"] if step["type"] == "service"]
        delays = [step["seconds"] for step in plan["steps"] if step["type"] == "delay"]
        self.assertEqual(services, ["turn_on", "turn_off", "turn_on", "turn_off", "turn_on"])
        self.assertEqual(delays, [2.0, 2.0, 2.0, 2.0])

    def test_scheduled_sequence_persists_completion_notification(self) -> None:
        answer = self.agent.try_schedule_known_switch_sequence(
            "dentro de 20s enciende la luz de la cocina y luego la apagaras y enciendes "
            "cada 2 segundos 2 veces; avisame cuando acabes"
        )

        self.assertIsNotNone(answer)
        plan = decode_plan(self.agent.memory.tasks[0]["instruction"])
        self.assertEqual(
            plan["completion_notification"]["message"],
            "Tarea completada: Secuencia de luz de la cocina",
        )

    def test_binary_pulse_is_compiled_as_historical_transition(self) -> None:
        answer = self.agent.try_schedule_binary_transition(
            "cuando el timbre se active enciende la luz de la cocina"
        )

        self.assertIsNotNone(answer)
        plan = decode_plan(self.agent.memory.tasks[0]["instruction"])
        condition = plan["conditions"][0]
        self.assertEqual(condition["type"], "transition")
        self.assertEqual(condition["entity_id"], "binary_sensor.automatismosf2_rob32_tecladoopen")
        self.assertEqual(condition["from_state"], "off")
        self.assertEqual(condition["to_state"], "on")
        self.assertEqual(condition["operator"], "gte")
        self.assertEqual(condition["value"], 1)

    def test_only_real_actions_satisfy_scheduled_execution(self) -> None:
        self.assertFalse(self.agent.is_execution_action_tool("__builtin__:ha_search_entities"))
        self.assertFalse(self.agent.is_execution_action_tool("__builtin__:ha_get_state"))
        self.assertTrue(self.agent.is_execution_action_tool("__builtin__:ha_call_service"))

    def test_repeated_short_wait_sequence_requires_one_scheduled_task(self) -> None:
        self.assertTrue(
            self.agent.requires_single_scheduled_sequence(
                "Enciende, espera 5 segundos y apaga la luz del comedor dentro de 1 minuto y lo repites 3 veces"
            )
        )
        self.assertFalse(
            self.agent.requires_single_scheduled_sequence(
                "Enciende la luz a las 23:00 y apágala a las 03:00"
            )
        )

    def test_historical_weekday_does_not_create_a_task(self) -> None:
        self.assertEqual(scheduling_intent_hint("consumo de energia el martes"), "")
        self.assertEqual(scheduling_intent_hint("que consumo tuve hoy"), "")
        self.assertEqual(scheduling_intent_hint("consumo de energia el 29 de mayo de este ano"), "")
        self.assertTrue(scheduling_intent_hint("enciende la luz el martes"))
        self.assertTrue(scheduling_intent_hint("el martes consulta el consumo de energia"))

    def test_weekday_resolution_uses_most_recent_or_previous_week(self) -> None:
        now = dt.datetime.fromisoformat("2026-07-16T12:00:00+02:00")
        self.assertEqual(str(requested_weekday_date("consumo de energia el martes", now)), "2026-07-14")
        self.assertEqual(
            str(requested_weekday_date("consumo de energia el martes de la semana pasada", now)),
            "2026-07-07",
        )
        self.assertEqual(
            str(requested_history_date("consumo de energia el dia 10 del mes anterior", now)),
            "2026-06-10",
        )
        self.assertIsNone(requested_history_date("consumo de energia el dia 31 del mes anterior", now))
        self.assertEqual(
            str(requested_history_date("consumo de energia el 29 de mayo de este ano", now)),
            "2026-05-29",
        )
        self.assertEqual(
            str(requested_history_date("consumo de energia el 29 de mayo de 2025", now)),
            "2025-05-29",
        )
        self.assertEqual(str(requested_history_date("consumo total de ayer", now)), "2026-07-15")
        self.assertEqual(str(requested_history_date("consumo total de anteayer", now)), "2026-07-14")
        self.assertEqual(str(requested_history_date("consumo total de hoy", now)), "2026-07-16")


class NumericWeekdayQueryTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def supply_rows() -> list[dict]:
        return [
            {
                "entity_id": "sensor.energiap_casa_diario",
                "friendly_name": "Energia casa diaria",
                "device_class": "energy",
                "unit": "kWh",
                "aliases": json.dumps(["consumo energia casa"]),
                "state": "5.4",
            },
            {
                "entity_id": "sensor.energiap_oficina_diario",
                "friendly_name": "Energia oficina diaria",
                "device_class": "energy",
                "unit": "kWh",
                "aliases": json.dumps(["consumo energia oficina"]),
                "state": "1.2",
            },
        ]

    async def test_unscoped_energy_consumption_sums_supply_groups(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)

        class FakeMemory:
            def search_entity_catalog(self, query: str, domain: str = "", limit: int = 20):
                return NumericWeekdayQueryTest.supply_rows()

        calls: list[dict] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append(args)
            value = 2.0 if "casa" in args["entity_id"] else 1.0
            return json.dumps({"results": [{"unit": "kWh", "periods": [{"value": value}]}]})

        agent.memory = FakeMemory()
        agent.call_builtin_tool = fake_call
        now = dt.datetime.fromisoformat("2026-07-16T12:00:00+02:00")
        with patch("codexon.local_now", return_value=now):
            answer = await agent.try_answer_numeric_weekday_value("consumo total de ayer")

        self.assertIn("consumo total suministrado", answer or "")
        self.assertIn("3,00 kWh", answer or "")
        self.assertIn("Casa 2,00", answer or "")
        self.assertIn("Oficina 1,00", answer or "")
        self.assertEqual({call["entity_id"] for call in calls}, {
            "sensor.energiap_casa_diario",
            "sensor.energiap_oficina_diario",
        })
        self.assertTrue(all(call["start_time"] == "2026-07-15T00:00:00+02:00" for call in calls))

    async def test_scoped_energy_consumption_uses_only_requested_group(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)

        class FakeMemory:
            def search_entity_catalog(self, query: str, domain: str = "", limit: int = 20):
                return NumericWeekdayQueryTest.supply_rows()

        calls: list[str] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append(args["entity_id"])
            return json.dumps({"results": [{"unit": "kWh", "periods": [{"value": 1.0}]}]})

        agent.memory = FakeMemory()
        agent.call_builtin_tool = fake_call
        now = dt.datetime.fromisoformat("2026-07-16T12:00:00+02:00")
        with patch("codexon.local_now", return_value=now):
            answer = await agent.try_answer_numeric_weekday_value("consumo de energia de la oficina el martes")

        self.assertNotIn("total suministrado", answer or "")
        self.assertIn("1,00 kWh", answer or "")
        self.assertEqual(calls, ["sensor.energiap_oficina_diario"])

    async def test_unscoped_weekly_comparison_uses_total_supply(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)

        class FakeMemory:
            def search_entity_catalog(self, query: str, domain: str = "", limit: int = 20):
                return NumericWeekdayQueryTest.supply_rows()

        async def fake_call(real_name: str, args: dict) -> str:
            day = dt.date.fromisoformat(args["start_time"][:10])
            if day == dt.date(2026, 7, 7):
                value = 1.0 if "casa" in args["entity_id"] else 4.0
            else:
                value = 2.0 if "casa" in args["entity_id"] else 1.0
            return json.dumps({"results": [{"unit": "kWh", "periods": [{"value": value}]}]})

        agent.memory = FakeMemory()
        agent.call_builtin_tool = fake_call
        now = dt.datetime.fromisoformat("2026-07-16T12:00:00+02:00")
        with patch("codexon.local_now", return_value=now):
            answer = await agent.try_answer_numeric_period_comparison(
                "que dia de la semana pasada consumi mas energia"
            )

        self.assertIn("martes 7 de julio", answer or "")
        self.assertIn("mayor consumo total suministrado", answer or "")
        self.assertIn("5,00 kWh", answer or "")

    async def test_energy_consumption_on_tuesday_uses_generic_history_tool(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)

        class FakeMemory:
            def search_entity_catalog(self, query: str, domain: str = "", limit: int = 20):
                return [{
                    "entity_id": "sensor.energiap_casa_diario",
                    "friendly_name": "Energia casa diaria",
                    "device_class": "energy",
                    "unit": "kWh",
                    "aliases": json.dumps(["consumo energia casa"]),
                    "state": "5.4",
                }]

        calls: list[tuple[str, dict]] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            return json.dumps({
                "results": [{
                    "unit": "kWh",
                    "periods": [{"period": "2026-07-14", "value": 6.25}],
                }]
            })

        agent.memory = FakeMemory()
        agent.call_builtin_tool = fake_call
        now = dt.datetime.fromisoformat("2026-07-16T12:00:00+02:00")
        with patch("codexon.local_now", return_value=now):
            answer = await agent.try_answer_numeric_weekday_value("consumo de energia el martes")

        self.assertIn("martes 14 de julio", answer or "")
        self.assertIn("6,25 kWh", answer or "")
        self.assertEqual(calls[0][0], "__builtin__:ha_aggregate_numeric_history")
        self.assertEqual(calls[0][1]["entity_id"], "sensor.energiap_casa_diario")
        self.assertEqual(calls[0][1]["start_time"], "2026-07-14T00:00:00+02:00")
        self.assertEqual(calls[0][1]["aggregation"], "max")

    async def test_energy_consumption_on_previous_month_day_uses_daily_range(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)

        class FakeMemory:
            def search_entity_catalog(self, query: str, domain: str = "", limit: int = 20):
                return [
                    {
                        "entity_id": "sensor.energiap_casa_diario",
                        "friendly_name": "Energia casa diaria",
                        "device_class": "energy",
                        "unit": "kWh",
                        "aliases": json.dumps(["consumo energia casa"]),
                        "state": "5.4",
                    },
                    {
                        "entity_id": "sensor.energiap_casa",
                        "friendly_name": "Energia casa acumulada",
                        "device_class": "energy",
                        "unit": "kWh",
                        "aliases": json.dumps(["consumo energia casa acumulado"]),
                        "state": "4100",
                    },
                ]

        calls: list[tuple[str, dict]] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_aggregate_numeric_history":
                return json.dumps({"results": [{"unit": "kWh", "periods": []}]})
            return json.dumps({
                "entity_id": args["entity_id"],
                "value": None if args["entity_id"].endswith("_diario") else 7.5,
            })

        agent.memory = FakeMemory()
        agent.call_builtin_tool = fake_call
        now = dt.datetime.fromisoformat("2026-07-16T12:00:00+02:00")
        with patch("codexon.local_now", return_value=now):
            answer = await agent.try_answer_numeric_weekday_value(
                "consumo de energia el dia 10 del mes anterior"
            )

        self.assertIn("miercoles 10 de junio", answer or "")
        self.assertIn("7,50 kWh", answer or "")
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1][1]["entity_id"], "sensor.energiap_casa")
        self.assertEqual(calls[1][1]["aggregation"], "delta")
        self.assertFalse(calls[1][1]["exclude_start_state"])
        self.assertEqual(calls[3][0], "__builtin__:ha_get_long_term_statistics")
        self.assertEqual(calls[3][1]["entity_id"], "sensor.energiap_casa")
        self.assertEqual(calls[3][1]["start_time"], "2026-06-10T00:00:00+02:00")
        self.assertEqual(calls[3][1]["end_time"], "2026-06-11T00:00:00+02:00")


class ScheduledLightCycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_alias_correction_is_persisted_without_llm(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            return json.dumps(
                {
                    "learned": True,
                    "target_entity_id": args["target_entity_id"],
                }
            )

        agent.call_builtin_tool = fake_call
        answer = await agent.try_apply_explicit_entity_teaching(
            "corrige el grifo del estanque por switch.riego2_rele3"
        )

        self.assertIn("Alias permanente guardado", answer)
        self.assertEqual(calls[0][0], "__builtin__:ha_teach_entity_mapping")
        self.assertEqual(calls[0][1]["operation"], "alias")
        self.assertEqual(calls[0][1]["alias"], "grifo del estanque")
        self.assertEqual(
            calls[0][1]["target_entity_id"], "switch.riego2_rele3"
        )

    async def test_explicit_device_replacement_is_persisted_without_llm(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            return json.dumps(
                {
                    "learned": True,
                    "old_entity_id": args["old_entity_id"],
                    "target_entity_id": args["target_entity_id"],
                }
            )

        agent.call_builtin_tool = fake_call
        answer = await agent.try_apply_explicit_entity_teaching(
            "cambia sensor.temperatura_viejo por sensor.temperatura_nuevo"
        )

        self.assertIn("Corrección permanente guardada", answer)
        self.assertEqual(calls[0][1]["operation"], "replace")
        self.assertEqual(
            calls[0][1]["old_entity_id"], "sensor.temperatura_viejo"
        )
        self.assertEqual(
            calls[0][1]["target_entity_id"], "sensor.temperatura_nuevo"
        )

    async def test_old_critical_scheduled_task_gets_default_completion_alert(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        agent.current_task_mobile_alert_sent = False
        calls: list[tuple[str, dict]] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            return json.dumps({"called": True})

        agent.call_builtin_tool = fake_call
        result = await agent.send_scheduled_completion_alert(
            title="Cerrar agua",
            instruction="Cierra la valvula general de agua",
            result="Valvula cerrada",
        )

        self.assertEqual(calls[0][0], "__builtin__:ha_send_mobile_alert")
        self.assertIn("Aviso móvil", result)

    async def test_scheduled_task_with_disabled_preference_does_not_alert(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        agent.current_task_mobile_alert_sent = False
        calls: list[tuple[str, dict]] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            return json.dumps({"called": True})

        agent.call_builtin_tool = fake_call
        instruction = encode_plan(
            {
                "version": 1,
                "name": "Cerrar agua",
                "conditions": [],
                "steps": [
                    {
                        "type": "service",
                        "domain": "switch",
                        "service": "turn_off",
                        "target": {"entity_id": "switch.valvula_agua"},
                    }
                ],
                "completion_notification": {"enabled": False},
            }
        )
        result = await agent.send_scheduled_completion_alert(
            title="Cerrar agua",
            instruction=instruction,
            result="Valvula cerrada",
        )

        self.assertEqual(result, "Valvula cerrada")
        self.assertEqual(calls, [])

    async def test_schedule_automation_persists_generic_plan(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)

        class FakeMemory:
            def __init__(self) -> None:
                self.rows: list[dict] = []

            def add_task(self, **kwargs) -> int:
                self.rows.append({"id": 1, **kwargs})
                return 1

            def list_tasks(self, **kwargs):
                return self.rows

        agent.memory = FakeMemory()
        result = json.loads(
            await agent.schedule_automation(
                {
                    "run_at": "2030-01-01T00:00:00+00:00",
                    "title": "Sensor binario",
                    "plan": {
                        "version": 1,
                        "name": "Sensor binario",
                        "conditions": [
                            {"entity_id": "binary_sensor.puerta", "operator": "eq", "value": "on"}
                        ],
                        "condition_policy": {"on_false": "reschedule", "delay_seconds": 10},
                        "steps": [
                            {
                                "type": "service",
                                "domain": "switch",
                                "service": "turn_on",
                                "target": {"entity_id": "switch.luz"},
                                "expected_state": "on",
                            }
                        ],
                    },
                }
            )
        )

        self.assertTrue(result["created"])
        self.assertIsNotNone(decode_plan(agent.memory.rows[0]["instruction"]))

    async def test_comedor_runs_three_verified_cycles_and_finishes_off(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        agent.site_profile = TEST_SITE_PROFILE
        calls: list[tuple[str, dict]] = []
        current_state = "off"

        async def fake_call(real_name: str, args: dict) -> str:
            nonlocal current_state
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_call_service":
                current_state = "on" if args["service"] == "turn_on" else "off"
                return json.dumps({"success": True})
            if real_name == "__builtin__:ha_get_state":
                return json.dumps({"entity_id": args["entity_id"], "state": current_state})
            if real_name == "__builtin__:wait_seconds":
                return json.dumps({"waited_seconds": args["seconds"]})
            raise AssertionError(real_name)

        agent.call_builtin_tool = fake_call
        result = await agent.try_execute_light_cycle_task(
            "Enciende, espera 5 segundos y apaga la luz del comedor dentro de 1 minuto y lo repites 3 veces"
        )

        services = [args["service"] for name, args in calls if name == "__builtin__:ha_call_service"]
        waits = [args["seconds"] for name, args in calls if name == "__builtin__:wait_seconds"]
        targets = [args["target"]["entity_id"] for name, args in calls if name == "__builtin__:ha_call_service"]
        self.assertEqual(services, ["turn_on", "turn_off"] * 3)
        self.assertEqual(len(waits), 3)
        for waited in waits:
            self.assertAlmostEqual(waited, 5.0, delta=0.05)
        self.assertEqual(targets, ["switch.relestftcomedor_rele1"] * 6)
        self.assertEqual(current_state, "off")
        self.assertIn("Ejecutados 3 ciclos", result)

    async def test_immediate_kitchen_sequence_runs_one_cycle_in_order(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        agent.site_profile = TEST_SITE_PROFILE
        calls: list[tuple[str, dict]] = []
        current_state = "off"

        async def fake_call(real_name: str, args: dict) -> str:
            nonlocal current_state
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_call_service":
                current_state = "on" if args["service"] == "turn_on" else "off"
                return json.dumps({"success": True})
            if real_name == "__builtin__:ha_get_state":
                return json.dumps({"entity_id": args["entity_id"], "state": current_state})
            if real_name == "__builtin__:wait_seconds":
                return json.dumps({"waited_seconds": args["seconds"]})
            raise AssertionError(real_name)

        agent.call_builtin_tool = fake_call
        result = await agent.try_execute_light_cycle_task(
            "Enciende la luz de la cocina, espera 10 segundos y apagala"
        )

        relevant = [
            (name, args.get("service") or args.get("seconds"))
            for name, args in calls
            if name in ("__builtin__:ha_call_service", "__builtin__:wait_seconds")
        ]
        self.assertEqual(relevant[0], ("__builtin__:ha_call_service", "turn_on"))
        self.assertEqual(relevant[2], ("__builtin__:ha_call_service", "turn_off"))
        self.assertEqual(relevant[1][0], "__builtin__:wait_seconds")
        self.assertAlmostEqual(float(relevant[1][1]), 10.0, delta=0.05)
        self.assertEqual(current_state, "off")
        self.assertIn("Ejecutados 1 ciclo", result)

    async def test_simple_kitchen_action_uses_switch_relay_2(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        agent.site_profile = TEST_SITE_PROFILE
        calls: list[tuple[str, dict]] = []
        current_state = "off"

        async def fake_call(real_name: str, args: dict) -> str:
            nonlocal current_state
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_call_service":
                current_state = "on"
                return json.dumps({"success": True})
            if real_name == "__builtin__:ha_get_state":
                return json.dumps({"entity_id": args["entity_id"], "state": current_state})
            raise AssertionError(real_name)

        agent.call_builtin_tool = fake_call
        result = await agent.try_execute_known_switch_action_task("Encender la luz de la cocina")

        action = next(args for name, args in calls if name == "__builtin__:ha_call_service")
        self.assertEqual(action["domain"], "switch")
        self.assertEqual(action["service"], "turn_on")
        self.assertEqual(action["target"]["entity_id"], "switch.nspanel_relay_2")
        self.assertIn("estado final on", result)

    async def test_deterministic_water_payload_is_not_treated_as_switch_action(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        agent.site_profile = TEST_SITE_PROFILE

        async def unexpected_call(real_name: str, args: dict) -> str:
            self.fail(f"No debía ejecutar {real_name}: {args}")

        agent.call_builtin_tool = unexpected_call
        instruction = (
            'DETERMINISTIC_WATER_LITERS {"switch_entity_id":"switch.riego2_rele1",'
            '"meter_entity_id":"sensor.controlh2oficina_acumulado_temporal_caudalimetro",'
            '"activation_mode_entity_id":"input_select.riego2_modo_activacion_zona1"}'
        )

        result = await agent.try_execute_known_switch_action_task(instruction)

        self.assertIsNone(result)

    async def test_structured_switch_sequence_preserves_all_alternations(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []
        current_state = "off"

        async def fake_call(real_name: str, args: dict) -> str:
            nonlocal current_state
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_call_service":
                current_state = "on" if args["service"] == "turn_on" else "off"
                return json.dumps({"success": True})
            if real_name == "__builtin__:ha_get_state":
                return json.dumps({"entity_id": args["entity_id"], "state": current_state})
            if real_name == "__builtin__:wait_seconds":
                return json.dumps({"waited_seconds": args["seconds"]})
            raise AssertionError(real_name)

        agent.call_builtin_tool = fake_call
        payload = {
            "entity_id": "switch.nspanel_relay_2",
            "label": "luz de la cocina",
            "initial_service": "turn_on",
            "repeat_services": ["turn_off", "turn_on"],
            "interval_seconds": 2,
            "repeat_count": 2,
        }
        result = await agent.try_execute_automation_plan(
            "DETERMINISTIC_SWITCH_SEQUENCE " + json.dumps(payload)
        )

        services = [args["service"] for name, args in calls if name == "__builtin__:ha_call_service"]
        waits = [args["seconds"] for name, args in calls if name == "__builtin__:wait_seconds"]
        self.assertEqual(services, ["turn_on", "turn_off", "turn_on", "turn_off", "turn_on"])
        self.assertEqual(len(waits), 4)
        for waited in waits:
            self.assertAlmostEqual(waited, 2.0, delta=0.05)
        self.assertEqual(current_state, "on")
        self.assertIn("5 servicios ejecutados", result)

    async def test_water_liters_task_reschedules_until_target_delta(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []
        reschedules: list[dict] = []
        switch_states = {
            "switch.riego_rele1": "off",
            "switch.riego_rele2": "off",
        }

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_get_state":
                entity_id = args["entity_id"]
                if entity_id == "input_number.lectura_total_compensada_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "102"})
                if entity_id == "sensor.piaoficina_power":
                    return json.dumps({"entity_id": entity_id, "state": "350"})
                if entity_id in switch_states:
                    return json.dumps({"entity_id": entity_id, "state": switch_states[entity_id]})
                return json.dumps({"entity_id": entity_id, "state": "off"})
            if real_name == "__builtin__:ha_call_service":
                entity_id = args["target"]["entity_id"]
                if entity_id in switch_states:
                    switch_states[entity_id] = "on" if args["service"] == "turn_on" else "off"
                return json.dumps({"called": True})
            if real_name == "__builtin__:ha_send_mobile_alert":
                return json.dumps({"called": True})
            if real_name == "__builtin__:wait_seconds":
                return json.dumps({"waited": args["seconds"]})
            raise AssertionError(real_name)

        async def fake_reschedule(args: dict) -> str:
            reschedules.append(args)
            return json.dumps({"rescheduled": True})

        agent.call_builtin_tool = fake_call
        agent.reschedule_current_task = fake_reschedule
        agent.current_task_id = 29
        payload = {
            "switch_entity_id": "switch.riego_rele2",
            "master_switch_entity_id": "switch.riego_rele1",
            "meter_entity_id": "input_number.lectura_total_compensada_caudalimetro",
            "target_liters": 10,
            "baseline_liters": 100,
            "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "check_interval_seconds": 10,
            "alarm_entity_ids": ["binary_sensor.riego_alarma_sin_agua"],
            "start_notified": True,
        }

        result = await agent.try_execute_water_liters_task(
            "DETERMINISTIC_WATER_LITERS " + json.dumps(payload)
        )

        services = [
            (args["target"]["entity_id"], args["service"])
            for name, args in calls
            if name == "__builtin__:ha_call_service"
        ]
        self.assertEqual(services, [("switch.riego_rele1", "turn_on"), ("switch.riego_rele2", "turn_on")])
        self.assertEqual(switch_states["switch.riego_rele1"], "on")
        self.assertEqual(switch_states["switch.riego_rele2"], "on")
        self.assertEqual(len(reschedules), 1)
        self.assertIn("DETERMINISTIC_WATER_LITERS", reschedules[0]["instruction"])
        self.assertIn("entregados 2.0/10", result)

    async def test_water_liters_office_does_not_retoggle_master_when_already_on(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []
        reschedules: list[dict] = []
        switch_states = {
            "switch.riego_rele1": "on",
            "switch.riego_rele7": "off",
        }

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_get_state":
                entity_id = args["entity_id"]
                if entity_id == "input_number.lectura_total_compensada_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "100"})
                if entity_id == "sensor.controlh2oficina_acumulado_temporal_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "0"})
                if entity_id == "input_number.riego_litros_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "60"})
                if entity_id == "sensor.riego_actual_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "Activo"})
                if entity_id == "sensor.riego_restante_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "10"})
                if entity_id in switch_states:
                    return json.dumps({"entity_id": entity_id, "state": switch_states[entity_id]})
                return json.dumps({"entity_id": entity_id, "state": "off"})
            if real_name == "__builtin__:ha_call_service":
                entity_id = args["target"]["entity_id"]
                if entity_id in switch_states:
                    switch_states[entity_id] = "on" if args["service"] == "turn_on" else "off"
                return json.dumps({"called": True})
            if real_name == "__builtin__:ha_send_mobile_alert":
                return json.dumps({"called": True})
            if real_name == "__builtin__:wait_seconds":
                return json.dumps({"waited": args["seconds"]})
            raise AssertionError(real_name)

        async def fake_reschedule(args: dict) -> str:
            reschedules.append(args)
            return json.dumps({"rescheduled": True})

        agent.call_builtin_tool = fake_call
        agent.reschedule_current_task = fake_reschedule
        agent.current_task_id = 31
        payload = agent.parse_water_liters_payload("regar 10 litros en oficina")

        result = await agent.try_execute_water_liters_task(
            "DETERMINISTIC_WATER_LITERS " + json.dumps(payload)
        )

        services = [
            (args["domain"], args["target"]["entity_id"], args["service"])
            for name, args in calls
            if name == "__builtin__:ha_call_service"
        ]
        self.assertEqual(
            services,
            [
                ("input_select", "input_select.riego_modo_activacion_zona7", "select_option"),
                ("input_select", "input_select.riego_modo_cantidad_zona7", "select_option"),
                ("input_number", "input_number.riego_litros_zona7", "set_value"),
                ("input_button", "input_button.riego_manual_zona7", "press"),
            ],
        )
        self.assertNotIn(("switch", "switch.riego_rele7", "turn_on"), services)
        self.assertEqual(len(reschedules), 1)
        self.assertIn("entregados 0.0/10", result)

    async def test_water_liters_office_uses_appdaemon_without_power_gate(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []
        reschedules: list[dict] = []
        switch_states = {
            "switch.riego_rele1": "on",
            "switch.riego_rele7": "off",
        }

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_get_state":
                entity_id = args["entity_id"]
                if entity_id == "input_number.lectura_total_compensada_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "100"})
                if entity_id == "sensor.controlh2oficina_acumulado_temporal_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "0"})
                if entity_id == "input_number.riego_litros_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "60"})
                if entity_id == "sensor.piaoficina_power":
                    return json.dumps({"entity_id": entity_id, "state": "80"})
                if entity_id == "sensor.riego_actual_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "Activo"})
                if entity_id == "sensor.riego_restante_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "10"})
                if entity_id in switch_states:
                    return json.dumps({"entity_id": entity_id, "state": switch_states[entity_id]})
                return json.dumps({"entity_id": entity_id, "state": "off"})
            if real_name == "__builtin__:ha_call_service":
                entity_id = args["target"]["entity_id"]
                if entity_id in switch_states:
                    switch_states[entity_id] = "on" if args["service"] == "turn_on" else "off"
                return json.dumps({"called": True})
            if real_name == "__builtin__:ha_send_mobile_alert":
                return json.dumps({"called": True})
            if real_name == "__builtin__:wait_seconds":
                return json.dumps({"waited": args["seconds"]})
            raise AssertionError(real_name)

        async def fake_reschedule(args: dict) -> str:
            reschedules.append(args)
            return json.dumps({"rescheduled": True})

        agent.call_builtin_tool = fake_call
        agent.reschedule_current_task = fake_reschedule
        agent.current_task_id = 32
        payload = agent.parse_water_liters_payload("regar 10 litros en oficina")

        result = await agent.try_execute_water_liters_task(
            "DETERMINISTIC_WATER_LITERS " + json.dumps(payload)
        )

        services = [
            (args["domain"], args["target"]["entity_id"], args["service"])
            for name, args in calls
            if name == "__builtin__:ha_call_service"
        ]
        self.assertIn(("input_button", "input_button.riego_manual_zona7", "press"), services)
        self.assertNotIn(("switch", "switch.riego_rele7", "turn_on"), services)
        self.assertEqual(len(reschedules), 1)
        self.assertIn("AppDaemon mantiene switch.riego_rele7", result)

    async def test_legacy_water_liters_payload_is_migrated_to_appdaemon(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []
        reschedules: list[dict] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_get_state":
                entity_id = args["entity_id"]
                if entity_id == "input_number.lectura_total_compensada_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "100"})
                if entity_id == "sensor.controlh2oficina_acumulado_temporal_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "0"})
                if entity_id == "input_number.riego_litros_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "60"})
                if entity_id == "sensor.piaoficina_power":
                    raise AssertionError("piaoficina_power no debe consultarse en tareas AppDaemon migradas")
                if entity_id == "sensor.riego_actual_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "Activo"})
                if entity_id == "sensor.riego_restante_zona7":
                    return json.dumps({"entity_id": entity_id, "state": "10"})
                return json.dumps({"entity_id": entity_id, "state": "off"})
            if real_name in {"__builtin__:ha_call_service", "__builtin__:ha_send_mobile_alert"}:
                return json.dumps({"called": True})
            if real_name == "__builtin__:wait_seconds":
                return json.dumps({"waited": args["seconds"]})
            raise AssertionError(real_name)

        async def fake_reschedule(args: dict) -> str:
            reschedules.append(args)
            return json.dumps({"rescheduled": True})

        agent.call_builtin_tool = fake_call
        agent.reschedule_current_task = fake_reschedule
        agent.current_task_id = 33
        legacy_payload = {
            "switch_entity_id": "switch.riego_rele7",
            "master_switch_entity_id": "switch.riego_rele1",
            "meter_entity_id": "input_number.lectura_total_compensada_caudalimetro",
            "target_liters": 10,
            "power_verification": {
                "sensor_entity_id": "sensor.piaoficina_power",
                "min_watts": 300,
                "wait_seconds": 3,
                "max_attempts": 2,
            },
            "required_valve_states": {"switch.controlh2oficina_relealmacen4": "off"},
        }

        result = await agent.try_execute_water_liters_task(
            "DETERMINISTIC_WATER_LITERS " + json.dumps(legacy_payload)
        )

        service_calls = [
            (args["domain"], args["target"]["entity_id"], args["service"])
            for name, args in calls
            if name == "__builtin__:ha_call_service"
        ]
        self.assertIn(("input_button", "input_button.riego_manual_zona7", "press"), service_calls)
        self.assertEqual(len(reschedules), 1)
        self.assertIn('"enabled":false', reschedules[0]["instruction"])
        self.assertIn("AppDaemon mantiene switch.riego_rele7", result)

    async def test_water_liters_task_requires_open_pass_valve_before_start(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_get_state":
                entity_id = args["entity_id"]
                if entity_id == "input_number.lectura_total_compensada_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "100"})
                if entity_id == "switch.controlh2oficina_relealmacen4":
                    return json.dumps({"entity_id": entity_id, "state": "on"})
                return json.dumps({"entity_id": entity_id, "state": "off"})
            if real_name in {"__builtin__:ha_call_service", "__builtin__:ha_send_mobile_alert"}:
                return json.dumps({"called": True})
            raise AssertionError(real_name)

        agent.call_builtin_tool = fake_call
        payload = {
            "switch_entity_id": "switch.riego_rele1",
            "meter_entity_id": "input_number.lectura_total_compensada_caudalimetro",
            "target_liters": 10,
            "required_valve_states": {"switch.controlh2oficina_relealmacen4": "off"},
        }

        with self.assertRaisesRegex(RuntimeError, "llave de paso"):
            await agent.try_execute_water_liters_task(
                "DETERMINISTIC_WATER_LITERS " + json.dumps(payload)
            )

        service_calls = [args for name, args in calls if name == "__builtin__:ha_call_service"]
        self.assertEqual(service_calls[0]["service"], "turn_off")
        self.assertNotIn("turn_on", [args["service"] for args in service_calls])

    async def test_water_liters_task_default_tolerates_slow_meter_without_no_progress_cutoff(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        calls: list[tuple[str, dict]] = []
        reschedules: list[dict] = []

        async def fake_call(real_name: str, args: dict) -> str:
            calls.append((real_name, args))
            if real_name == "__builtin__:ha_get_state":
                entity_id = args["entity_id"]
                if entity_id == "input_number.lectura_total_compensada_caudalimetro":
                    return json.dumps({"entity_id": entity_id, "state": "100"})
                if entity_id == "switch.riego_rele1":
                    return json.dumps({"entity_id": entity_id, "state": "on"})
                return json.dumps({"entity_id": entity_id, "state": "off"})
            if real_name in {"__builtin__:ha_call_service", "__builtin__:ha_send_mobile_alert"}:
                return json.dumps({"called": True})
            raise AssertionError(real_name)

        async def fake_reschedule(args: dict) -> str:
            reschedules.append(args)
            return json.dumps({"rescheduled": True})

        agent.call_builtin_tool = fake_call
        agent.reschedule_current_task = fake_reschedule
        agent.current_task_id = 30
        old_time = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat(timespec="seconds")
        payload = {
            "switch_entity_id": "switch.riego_rele1",
            "meter_entity_id": "input_number.lectura_total_compensada_caudalimetro",
            "target_liters": 10,
            "baseline_liters": 100,
            "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "last_liters": 100,
            "last_progress_at": old_time,
            "check_interval_seconds": 10,
            "max_no_progress_seconds": 0,
            "alarm_entity_ids": ["binary_sensor.riego_alarma_sin_agua"],
            "start_notified": True,
        }

        result = await agent.try_execute_water_liters_task(
            "DETERMINISTIC_WATER_LITERS " + json.dumps(payload)
        )

        self.assertEqual(len(reschedules), 1)
        self.assertIn("queda encendido", result)
        services = [args["service"] for name, args in calls if name == "__builtin__:ha_call_service"]
        self.assertNotIn("turn_off", services)


if __name__ == "__main__":
    unittest.main()
