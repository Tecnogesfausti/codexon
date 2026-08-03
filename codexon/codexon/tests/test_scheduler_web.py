from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import codexon_web


class SchedulerWebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_db_path = codexon_web.DB_PATH
        self.previous_agents_dir = codexon_web.AGENTS_DIR
        codexon_web.DB_PATH = Path(self.tempdir.name) / "web-scheduler.sqlite3"
        codexon_web.AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"

    def tearDown(self) -> None:
        codexon_web.DB_PATH = self.previous_db_path
        codexon_web.AGENTS_DIR = self.previous_agents_dir
        self.tempdir.cleanup()

    def test_create_task_validates_and_marks_physical_action_manual(self) -> None:
        created = codexon_web.api_create_task(
            {
                "title": "Prueba web",
                "instruction": "encender switch.test",
                "run_at": "2030-01-01T00:00:00+00:00",
                "priority": 90,
                "interval_seconds": 60,
                "max_attempts": 4,
                "retry_backoff_seconds": 5,
            }
        )
        task = codexon_web.api_tasks()[0]

        self.assertEqual(task["id"], created["id"])
        self.assertEqual(task["priority"], 90)
        self.assertEqual(task["max_attempts"], 4)
        self.assertEqual(task["retry_backoff_seconds"], 5)
        self.assertEqual(task["recovery_policy"], "manual")

    def test_rejects_invalid_interval_and_status(self) -> None:
        with self.assertRaises(HTTPException):
            codexon_web.api_create_task(
                {
                    "title": "Intervalo inválido",
                    "instruction": "hacer algo",
                    "interval_seconds": -1,
                }
            )

        task_id = codexon_web.api_create_task(
            {"title": "Estado", "instruction": "hacer algo"}
        )["id"]
        with self.assertRaises(HTTPException):
            codexon_web.api_update_task(task_id, {"status": "running"})

    def test_delete_all_tasks_clears_tasks_and_runs(self) -> None:
        first = codexon_web.api_create_task({"title": "Uno", "instruction": "recordarme uno"})["id"]
        codexon_web.api_create_task({"title": "Dos", "instruction": "recordarme dos"})
        conn = codexon_web.connect_db()
        try:
            conn.execute(
                """
                INSERT INTO task_runs(task_id, worker_id, attempt, started_at, status)
                VALUES (?, 'test', 1, ?, 'done')
                """,
                (first, codexon_web.utc_now()),
            )
            conn.commit()
        finally:
            conn.close()

        result = codexon_web.api_delete_all_tasks()

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(codexon_web.api_tasks(), [])
        self.assertEqual(codexon_web.sqlite_rows("SELECT * FROM task_runs"), [])

    def test_event_listener_can_be_listed_cancelled_reenabled_and_audited(self) -> None:
        created = codexon_web.api_create_event_listener(
            {
                "title": "Cocina a sofá",
                "instruction": "AUTOMATION_PLAN_V1 test",
                "entity_id": "switch.cocina",
                "from_state": "off",
                "to_state": "on",
                "cooldown_seconds": 5,
            }
        )
        listener_id = created["id"]
        listener = next(
            row for row in codexon_web.api_event_listeners() if row["id"] == listener_id
        )
        self.assertEqual(listener["enabled"], 1)
        self.assertEqual(listener["entity_id"], "switch.cocina")

        self.assertEqual(
            codexon_web.api_cancel_event_listener(listener_id),
            {"ok": True, "id": listener_id},
        )
        self.assertEqual(
            next(
                row
                for row in codexon_web.api_event_listeners()
                if row["id"] == listener_id
            )["enabled"],
            0,
        )
        self.assertEqual(
            codexon_web.api_enable_event_listener(listener_id),
            {"ok": True, "id": listener_id},
        )

        conn = codexon_web.connect_db()
        try:
            conn.execute(
                """
                INSERT INTO event_trigger_runs(
                    subscription_id, triggered_at, event_type, entity_id, task_id
                )
                VALUES (?, ?, 'state_changed', 'switch.cocina', 42)
                """,
                (listener_id, codexon_web.utc_now()),
            )
            conn.commit()
        finally:
            conn.close()
        runs = codexon_web.api_event_listener_runs(listener_id)
        self.assertEqual(runs[0]["task_id"], 42)
        self.assertEqual(runs[0]["entity_id"], "switch.cocina")

    def test_dashboard_contains_event_listener_controls(self) -> None:
        dashboard = codexon_web.index()
        self.assertIn('data-tab="escuchas"', dashboard)
        self.assertIn('id="listeners"', dashboard)
        self.assertIn("cancelListener", dashboard)
        self.assertIn("enableListener", dashboard)
        self.assertIn("loadListenerRuns", dashboard)
        self.assertIn('class="task-select"', dashboard)
        self.assertIn('class="listener-select"', dashboard)
        self.assertIn("deleteSelectedTasks", dashboard)
        self.assertIn("deleteSelectedListeners", dashboard)
        self.assertIn("function loadTasks()", dashboard)
        self.assertIn("/api/tasks?limit=200&include_done=true", dashboard)
        self.assertIn('id="taskSummary"', dashboard)

    def test_dashboard_offers_four_independent_model_targets(self) -> None:
        dashboard = codexon_web.index()
        self.assertIn("Conversacion, HA y memoria", dashboard)
        self.assertIn("classification", dashboard)
        self.assertIn("statistical_planning", dashboard)
        self.assertIn("statistical_reasoning", dashboard)
        self.assertIn("modelTargetOptions", dashboard)

    def test_web_ha_base_url_accepts_addon_ha_url_without_duplicate_api(self) -> None:
        with patch.dict("os.environ", {"HA_URL": "http://homeassistant:8123/api"}, clear=False):
            with patch.dict("os.environ", {"HOME_ASSISTANT_URL": "", "HA_BASE_URL": ""}, clear=False):
                self.assertEqual(codexon_web.web_ha_base_url(), "http://homeassistant:8123")

    def test_dashboard_loads_agents_independently_and_explains_scheduler_mode(self) -> None:
        dashboard = codexon_web.index()
        self.assertIn("function loadAgents()", dashboard)
        self.assertIn('id="agentSummary"', dashboard)
        self.assertIn("El núcleo lo ejecuta periódicamente", dashboard)
        self.assertIn("Probar ahora", dashboard)
        self.assertIn("Activar", dashboard)

    def test_agents_api_explains_purpose_and_real_execution_mode(self) -> None:
        result = codexon_web.api_agents()

        self.assertGreater(len(result["agents"]), 0)
        for agent in result["agents"]:
            self.assertTrue(agent["purpose"])
            self.assertTrue(agent["inputs"])
            self.assertTrue(agent["output"])
            self.assertIn(agent["execution_mode"], {"paused", "scheduled"})
            self.assertFalse(agent["uses_llm"])
            self.assertIn("observation_count", agent)
            self.assertIn("last_observation", agent)

    def test_automatic_model_selection_only_clears_requested_target(self) -> None:
        codexon_web.set_setting("interactive_model", "general/model")
        codexon_web.set_setting("classification_model", "classifier/model")

        result = __import__("asyncio").run(
            codexon_web.api_select_model(
                {"model": "auto", "target": "classification"}
            )
        )

        self.assertIsNone(result["selected_model"])
        self.assertEqual(codexon_web.get_setting("interactive_model"), "general/model")
        self.assertEqual(codexon_web.get_setting("classification_model"), "")

    def test_bulk_delete_tasks_only_removes_selected_tasks_and_runs(self) -> None:
        first = codexon_web.api_create_task(
            {"title": "Uno", "instruction": "recordarme uno"}
        )["id"]
        second = codexon_web.api_create_task(
            {"title": "Dos", "instruction": "recordarme dos"}
        )["id"]
        conn = codexon_web.connect_db()
        try:
            for task_id in (first, second):
                conn.execute(
                    """
                    INSERT INTO task_runs(task_id, worker_id, attempt, started_at, status)
                    VALUES (?, 'test', 1, ?, 'done')
                    """,
                    (task_id, codexon_web.utc_now()),
                )
            conn.commit()
        finally:
            conn.close()

        result = codexon_web.api_bulk_delete_tasks({"ids": [first]})

        self.assertEqual(result["deleted"], 1)
        self.assertEqual([row["id"] for row in codexon_web.api_tasks()], [second])
        self.assertEqual(
            [row["task_id"] for row in codexon_web.sqlite_rows("SELECT task_id FROM task_runs")],
            [second],
        )

    def test_bulk_delete_listeners_removes_selected_history(self) -> None:
        first = codexon_web.api_create_event_listener(
            {
                "title": "Primera",
                "instruction": "hacer uno",
                "entity_id": "switch.uno",
            }
        )["id"]
        second = codexon_web.api_create_event_listener(
            {
                "title": "Segunda",
                "instruction": "hacer dos",
                "entity_id": "switch.dos",
            }
        )["id"]
        conn = codexon_web.connect_db()
        try:
            for listener_id in (first, second):
                conn.execute(
                    """
                    INSERT INTO event_trigger_runs(
                        subscription_id, triggered_at, event_type, entity_id
                    )
                    VALUES (?, ?, 'state_changed', 'switch.prueba')
                    """,
                    (listener_id, codexon_web.utc_now()),
                )
            conn.commit()
        finally:
            conn.close()

        result = codexon_web.api_bulk_delete_event_listeners({"ids": [first]})

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(
            [row["id"] for row in codexon_web.api_event_listeners()], [second]
        )
        self.assertEqual(codexon_web.api_event_listener_runs(first), [])


if __name__ == "__main__":
    unittest.main()
