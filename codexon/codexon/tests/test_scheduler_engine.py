from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from automation import decode_plan, encode_plan
from codexon import MemoryStore, next_interval_run_at


class SchedulerEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "scheduler.sqlite3"
        self.store = MemoryStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def add_due_task(self, **kwargs: object) -> int:
        return self.store.add_task(
            run_at="2020-01-01T00:00:00+00:00",
            title=str(kwargs.pop("title", "Prueba")),
            instruction=str(kwargs.pop("instruction", "encender switch.test")),
            **kwargs,
        )

    def task(self, task_id: int) -> sqlite3.Row:
        row = self.store.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row is not None
        return row

    def make_due(self, task_id: int) -> sqlite3.Row:
        self.store.conn.execute(
            "UPDATE tasks SET run_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (task_id,),
        )
        self.store.conn.commit()
        rows = self.store.get_due_tasks(limit=10)
        return next(row for row in rows if int(row["id"]) == task_id)

    def test_fresh_schema_contains_scheduler_columns_and_run_audit(self) -> None:
        columns = {row["name"] for row in self.store.conn.execute("PRAGMA table_info(tasks)")}
        self.assertTrue(
            {
                "priority",
                "interval_seconds",
                "attempts",
                "max_attempts",
                "retry_backoff_seconds",
                "lease_owner",
                "lease_expires_at",
                "recovery_policy",
            }.issubset(columns)
        )
        self.assertIsNotNone(
            self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'task_runs'"
            ).fetchone()
        )

    def test_claim_is_atomic_between_workers(self) -> None:
        task_id = self.add_due_task()
        other = MemoryStore(self.db_path)
        try:
            run_id = self.store.mark_task_running(task_id, "worker-a")
            self.assertIsNotNone(run_id)
            self.assertIsNone(other.mark_task_running(task_id, "worker-b"))
        finally:
            other.close()

    def test_cancelled_running_task_cannot_be_resurrected(self) -> None:
        task_id = self.add_due_task()
        run_id = self.store.mark_task_running(task_id, "worker-a")
        assert run_id is not None

        self.assertTrue(self.store.cancel_task(task_id))
        self.assertFalse(
            self.store.reschedule_running_task(
                task_id=task_id,
                worker_id="worker-a",
                run_id=run_id,
                run_at="2030-01-01T00:00:00+00:00",
            )
        )
        self.assertEqual(self.task(task_id)["status"], "cancelled")

    def test_running_worker_can_persist_transition_cursor_atomically(self) -> None:
        instruction = encode_plan(
            {
                "version": 1,
                "name": "Cursor",
                "conditions": [
                    {
                        "type": "transition",
                        "entity_id": "binary_sensor.puerta",
                        "from_state": "off",
                        "to_state": "on",
                        "after": "2020-01-01T00:00:00+00:00",
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
                    }
                ],
            }
        )
        task_id = self.add_due_task(instruction=instruction)
        run_id = self.store.mark_task_running(task_id, "worker-a")
        assert run_id is not None
        plan = decode_plan(instruction)
        plan["conditions"][0]["after"] = "2020-01-01T00:01:00+00:00"

        self.assertTrue(
            self.store.update_running_task_instruction(task_id, "worker-a", encode_plan(plan))
        )
        self.assertFalse(
            self.store.update_running_task_instruction(task_id, "worker-b", instruction)
        )
        self.assertEqual(
            decode_plan(self.task(task_id)["instruction"])["conditions"][0]["after"],
            "2020-01-01T00:01:00.000000+00:00",
        )

    def test_one_shot_retries_then_fails(self) -> None:
        task_id = self.add_due_task(
            instruction="consultar un dato remoto",
            max_attempts=3,
            retry_backoff_seconds=1,
        )
        for expected_attempt in (1, 2, 3):
            task = self.make_due(task_id)
            run_id = self.store.mark_task_running(task_id, "worker-a")
            assert run_id is not None
            outcome = self.store.fail_or_retry_task(task, "worker-a", run_id, "fallo")
            self.assertEqual(outcome["attempts"], expected_attempt)

        self.assertEqual(self.task(task_id)["status"], "failed")
        runs = list(self.store.conn.execute("SELECT status FROM task_runs WHERE task_id = ?", (task_id,)))
        self.assertEqual([row["status"] for row in runs], ["failed", "failed", "failed"])

    def test_physical_action_is_not_retried_after_uncertain_failure(self) -> None:
        task_id = self.add_due_task(
            instruction="Encender la luz de la cocina",
            max_attempts=3,
            retry_backoff_seconds=1,
        )
        task = self.make_due(task_id)
        run_id = self.store.mark_task_running(task_id, "worker-a")
        assert run_id is not None

        outcome = self.store.fail_or_retry_task(task, "worker-a", run_id, "resultado incierto")

        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["attempts"], 1)
        self.assertEqual(self.task(task_id)["recovery_policy"], "manual")

    def test_recurring_task_skips_failed_occurrence_after_retries(self) -> None:
        task_id = self.add_due_task(
            instruction="consultar un dato remoto cada minuto",
            interval_seconds=60,
            max_attempts=2,
            retry_backoff_seconds=1,
        )
        for _ in range(2):
            task = self.make_due(task_id)
            run_id = self.store.mark_task_running(task_id, "worker-a")
            assert run_id is not None
            self.store.fail_or_retry_task(task, "worker-a", run_id, "fallo recurrente")

        row = self.task(task_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertIn("Se agotaron los reintentos", row["last_error"])

    def test_interrupted_non_idempotent_one_shot_requires_manual_review(self) -> None:
        task_id = self.add_due_task(
            instruction="Pulsa button.terreno_fireworks_dispara_sirena",
        )
        run_id = self.store.mark_task_running(task_id, "worker-a")
        assert run_id is not None
        self.store.conn.execute(
            "UPDATE tasks SET lease_expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (task_id,),
        )
        self.store.conn.commit()

        self.assertEqual(self.store.recover_expired_tasks(), 1)
        row = self.task(task_id)
        self.assertEqual(row["status"], "failed")
        self.assertIn("resultado externo incierto", row["last_error"].lower())

    def test_interval_calculation_skips_long_downtime_without_looping(self) -> None:
        previous = (dt.datetime.now(dt.UTC) - dt.timedelta(days=365)).isoformat(timespec="seconds")
        started = dt.datetime.now(dt.UTC)
        result = dt.datetime.fromisoformat(next_interval_run_at(previous, 1))
        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()

        self.assertLess(elapsed, 0.1)
        self.assertGreater(result, dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        self.assertLessEqual(result, dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1))


if __name__ == "__main__":
    unittest.main()
