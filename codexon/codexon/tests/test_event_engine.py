from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from automation import decode_plan, encode_plan
from event_engine import (
    EventEngine,
    compile_state_action_listener,
    event_matches_subscription,
    is_listener_cancel_request,
    select_listener_candidate,
)
from event_engine.storage import (
    create_subscription,
    ensure_event_schema,
    list_subscriptions,
)


class FakeMemory:
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        ensure_event_schema(self.conn)
        self.conn.execute(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                instruction TEXT NOT NULL,
                cancellation_key TEXT,
                priority INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def add_task(
        self,
        *,
        run_at: str,
        title: str,
        instruction: str,
        cancellation_key: str | None,
        priority: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO tasks(run_at, status, title, instruction, cancellation_key, priority)
            VALUES (?, 'pending', ?, ?, ?, ?)
            """,
            (run_at, title, instruction, cancellation_key, priority),
        )
        self.conn.commit()
        return int(cursor.lastrowid)


def state_event(entity_id: str, old: str, new: str, **attributes: object) -> dict[str, object]:
    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity_id,
            "old_state": {"state": old, "attributes": {}},
            "new_state": {"state": new, "attributes": attributes},
        },
    }


class EventEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = FakeMemory(Path(self.tempdir.name) / "events.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_state_transition_and_numeric_attribute_match(self) -> None:
        create_subscription(
            self.store.conn,
            title="Temperatura alta",
            instruction="avisa",
            entity_id="climate.salon",
            from_state="heat",
            to_state="heat",
            attribute="current_temperature",
            operator="gte",
            expected_value=25,
        )
        row = list_subscriptions(self.store.conn, enabled_only=True)[0]
        self.assertTrue(
            event_matches_subscription(
                row, state_event("climate.salon", "heat", "heat", current_temperature=25.2)
            )
        )
        self.assertFalse(
            event_matches_subscription(
                row, state_event("climate.salon", "heat", "heat", current_temperature=24.9)
            )
        )

    def test_dispatch_queues_task_and_once_listener_is_disabled(self) -> None:
        listener_id = create_subscription(
            self.store.conn,
            title="Fuga",
            instruction="cierra la valvula y avisa",
            entity_id="binary_sensor.fuga",
            from_state="off",
            to_state="on",
            once_only=True,
        )
        logs: list[tuple[object, ...]] = []
        agent = SimpleNamespace(memory=self.store)
        engine = EventEngine(agent, runtime_log=lambda *args, **kwargs: logs.append(args))
        event = state_event("binary_sensor.fuga", "off", "on")
        self.assertEqual(asyncio.run(engine.dispatch(event)), 1)
        self.assertEqual(asyncio.run(engine.dispatch(event)), 0)
        listener = self.store.conn.execute(
            "SELECT enabled, trigger_count FROM event_subscriptions WHERE id = ?",
            (listener_id,),
        ).fetchone()
        self.assertEqual(listener["enabled"], 0)
        self.assertEqual(listener["trigger_count"], 1)
        task = self.store.conn.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(task["status"], "pending")
        self.assertIn("cierra la valvula", task["instruction"])
        run = self.store.conn.execute("SELECT * FROM event_trigger_runs").fetchone()
        self.assertEqual(run["task_id"], task["id"])

    def test_custom_event_data_filter(self) -> None:
        create_subscription(
            self.store.conn,
            title="Alarma",
            instruction="avisa",
            event_type="codexon_alarm",
            event_data={"zone": "garage", "severity": {"level": "high"}},
        )
        row = list_subscriptions(self.store.conn, enabled_only=True)[0]
        self.assertTrue(
            event_matches_subscription(
                row,
                {
                    "event_type": "codexon_alarm",
                    "data": {
                        "zone": "garage",
                        "severity": {"level": "high", "score": 9},
                    },
                },
            )
        )
        self.assertFalse(
            event_matches_subscription(
                row,
                {"event_type": "codexon_alarm", "data": {"zone": "garden"}},
            )
        )

    def test_cooldown_prevents_duplicate_tasks(self) -> None:
        create_subscription(
            self.store.conn,
            title="Movimiento",
            instruction="registra movimiento",
            entity_id="binary_sensor.movimiento",
            to_state="on",
            cooldown_seconds=60,
        )
        engine = EventEngine(
            SimpleNamespace(memory=self.store), runtime_log=lambda *args, **kwargs: None
        )
        event = state_event("binary_sensor.movimiento", "off", "on")
        self.assertEqual(asyncio.run(engine.dispatch(event)), 1)
        self.assertEqual(asyncio.run(engine.dispatch(event)), 0)
        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1
        )

    def test_simple_light_request_compiles_to_deterministic_plan(self) -> None:
        aliases = {
            "la luz de la cocina": (
                "switch.nspanel_relay_2",
                "luz de la cocina",
            ),
            "luz del comedor": (
                "switch.relestftcomedor_rele1",
                "luz del comedor",
            ),
        }
        compiled = compile_state_action_listener(
            "Cuando encienda la luz de la cocina enciende la del comedor",
            resolve_entity=aliases.get,
        )
        self.assertIsNotNone(compiled)
        assert compiled is not None
        self.assertEqual(compiled.entity_id, "switch.nspanel_relay_2")
        self.assertEqual((compiled.from_state, compiled.to_state), ("off", "on"))
        step = compiled.plan["steps"][0]
        self.assertEqual(step["domain"], "switch")
        self.assertEqual(step["service"], "turn_on")
        self.assertEqual(
            step["target"]["entity_id"], "switch.relestftcomedor_rele1"
        )
        self.assertTrue(step["confirm"])
        self.assertEqual(
            decode_plan(encode_plan(compiled.plan))["steps"][0], step
        )

    def test_semantic_listener_cancellation_selects_kitchen_to_sofa(self) -> None:
        phrase = "Cancela la automatización que enciende el sofá al encender la cocina"
        self.assertTrue(is_listener_cancel_request(phrase))
        selected = select_listener_candidate(
            phrase,
            [
                (
                    4,
                    "luz cocina off on enciende switch.nspanel_relay_2 "
                    "turn_on luz sofa switch.nspanel_relay_1",
                ),
                (8, "movimiento garaje on envia alerta movil"),
            ],
        )
        self.assertEqual(selected, 4)

    def test_gate_action_is_not_listener_cancellation(self) -> None:
        self.assertFalse(is_listener_cancel_request("Abre la cancela de la entrada"))
        self.assertFalse(
            is_listener_cancel_request("No canceles la automatización de cocina")
        )
