from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import unittest
from types import SimpleNamespace

from agents.base import AgentContext
from agents.especialista_agua_riego import EspecialistaAguaRiegoAgent
from agents.especialista_aprendizaje_hogar import EspecialistaAprendizajeHogarAgent
from agents.especialista_confort_climatico import EspecialistaConfortClimaticoAgent
from agents.especialista_estado_tecnico import EspecialistaEstadoTecnicoAgent
from agents.especialista_presencia_seguridad import EspecialistaPresenciaSeguridadAgent
from agents.especialista_verificador_acciones import EspecialistaVerificadorAccionesAgent
from codexon import CodexonAgent


NOW = dt.datetime(2026, 8, 2, 15, 0, tzinfo=dt.UTC)


def state(entity_id: str, value: str, *, name: str = "", unit: str = "", device_class: str = "") -> dict:
    return {
        "entity_id": entity_id,
        "state": value,
        "last_updated": NOW.isoformat(),
        "attributes": {"friendly_name": name, "unit_of_measurement": unit, "device_class": device_class},
    }


class FakeHA:
    def __init__(self, states: list[dict]) -> None:
        self.states = states

    async def get_states(self) -> list[dict]:
        return self.states


class FakeMemory:
    def __init__(self) -> None:
        self.observations: list[dict] = []
        self.settings: dict[str, str] = {}

    def add_observation(self, **kwargs) -> None:
        self.observations.append(kwargs)

    def get_setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


def context(states: list[dict], memory=None) -> AgentContext:
    return AgentContext(now=NOW, services={"ha_client": FakeHA(states), "memory": memory or FakeMemory()})


class SpecialistAgentsTest(unittest.TestCase):
    def test_presence_fuses_multiple_modalities_into_common_contract(self) -> None:
        memory = FakeMemory()
        agent_context = context([
            state("binary_sensor.camara_person_detection", "on", name="Cámara persona"),
            state("binary_sensor.sonar_presencia", "on", name="Radar presencia"),
        ], memory)
        agent = EspecialistaPresenciaSeguridadAgent()
        result = asyncio.run(agent.run(agent_context))
        repeated = asyncio.run(agent.run(agent_context))
        finding = result.data["finding"]
        self.assertEqual(result.data["contract"], "codexon.agent_finding.v1")
        self.assertEqual(finding["category"], "presence_security")
        self.assertGreaterEqual(finding["confidence"], 0.7)
        self.assertEqual(result.data["active_count"], 2)
        self.assertTrue(repeated.data["deduplicated"])
        self.assertEqual(len(memory.observations), 1)

    def test_water_agent_reports_active_valve_and_alarm(self) -> None:
        result = asyncio.run(EspecialistaAguaRiegoAgent().run(context([
            state("switch.riego2_rele1", "on"),
            state("binary_sensor.riego_conflicto_caudal", "on"),
            state("sensor.riego2_ultimo_riego_zona1", "20.5 L"),
        ])))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["active_valves"], 1)
        self.assertEqual(result.data["active_alarms"], 1)
        self.assertEqual(result.data["finding"]["urgency"], "critical")

    def test_comfort_agent_detects_hot_and_humid_conditions(self) -> None:
        result = asyncio.run(EspecialistaConfortClimaticoAgent().run(context([
            state("sensor.salon_temperatura", "31", unit="°C", device_class="temperature"),
            state("sensor.salon_humedad", "78", unit="%", device_class="humidity"),
            state("sensor.salon_co2", "1250", unit="ppm", device_class="carbon_dioxide"),
        ])))
        self.assertEqual(result.data["problem_count"], 3)
        self.assertEqual(result.data["finding"]["urgency"], "warning")

    def test_technical_agent_groups_unavailable_and_low_battery(self) -> None:
        result = asyncio.run(EspecialistaEstadoTecnicoAgent().run(context([
            state("sensor.exterior", "unavailable"),
            state("sensor.mando_bateria", "12", unit="%", device_class="battery"),
        ])))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["unavailable"], 1)
        self.assertEqual(result.data["low_battery"], 1)

    def test_verifier_compares_expected_and_actual_state(self) -> None:
        memory = FakeMemory()
        memory.settings["agents.pending_verifications"] = json.dumps([
            {"entity_id": "light.cocina", "expected_state": "on"},
            {"entity_id": "switch.bomba", "expected_state": "off"},
        ])
        result = asyncio.run(EspecialistaVerificadorAccionesAgent().run(context([
            state("light.cocina", "on"), state("switch.bomba", "on")
        ], memory)))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["verified"]), 1)
        self.assertEqual(len(result.data["failed"]), 1)
        self.assertEqual(memory.settings["agents.pending_verifications"], "[]")

    def test_successful_ha_action_registers_expected_state(self) -> None:
        memory = FakeMemory()
        CodexonAgent.remember_action_verifications(
            SimpleNamespace(memory=memory),
            {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.cocina"},
            },
        )
        pending = json.loads(memory.settings["agents.pending_verifications"])
        self.assertEqual(pending[0]["entity_id"], "light.cocina")
        self.assertEqual(pending[0]["expected_state"], "on")

    def test_learning_agent_detects_ambiguous_alias(self) -> None:
        memory = FakeMemory()
        memory.conn = sqlite3.connect(":memory:")
        memory.conn.row_factory = sqlite3.Row
        memory.conn.executescript("""
            CREATE TABLE entity_catalog(entity_id TEXT PRIMARY KEY);
            CREATE TABLE entity_aliases(entity_id TEXT, normalized_alias TEXT, priority INTEGER);
            CREATE TABLE entity_locations(entity_id TEXT);
            CREATE TABLE entity_roles(entity_id TEXT);
            CREATE TABLE entity_teachings(teaching_type TEXT, key_text TEXT, target_entity_id TEXT, active INTEGER);
            INSERT INTO entity_catalog VALUES ('switch.uno');
            INSERT INTO entity_aliases VALUES ('switch.uno', 'grifo', 90);
            INSERT INTO entity_aliases VALUES ('switch.dos', 'grifo', 90);
        """)
        result = asyncio.run(EspecialistaAprendizajeHogarAgent().run(AgentContext(now=NOW, services={"memory": memory})))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["alias_conflicts"]), 1)
