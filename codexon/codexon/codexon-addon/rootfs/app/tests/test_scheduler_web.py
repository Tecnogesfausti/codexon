from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

import codexon_web


class SchedulerWebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_db_path = codexon_web.DB_PATH
        codexon_web.DB_PATH = Path(self.tempdir.name) / "web-scheduler.sqlite3"

    def tearDown(self) -> None:
        codexon_web.DB_PATH = self.previous_db_path
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


if __name__ == "__main__":
    unittest.main()
