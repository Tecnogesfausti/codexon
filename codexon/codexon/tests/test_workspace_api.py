from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from workspace_api import EXCHANGE_RATE, RUNS, OpenCodeRun, absorb_event, command_for_run, environment_for_run, reconcile_usage, router


class WorkspaceApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "workspace"
        self.root.mkdir()
        self.outside = Path(self.tempdir.name) / "outside.txt"
        self.outside.write_text("outside", encoding="utf-8")
        self.environment = patch.dict(os.environ, {"CODEXON_FS_ROOTS": str(self.root)})
        self.environment.start()
        app = FastAPI()
        app.include_router(router)
        self.app = app
        self.client = TestClient(app)
        RUNS.clear()
        EXCHANGE_RATE.update(usd_per_eur=None, date=None, expires_at=0.0)

    def tearDown(self) -> None:
        self.client.close()
        RUNS.clear()
        self.environment.stop()
        self.tempdir.cleanup()

    def test_navigation_upload_download_and_existing_file(self) -> None:
        created = self.client.post("/api/workspace/folders", json={"directory": str(self.root), "name": "curso"})
        self.assertEqual(created.status_code, 200)
        folder = self.root / "curso"
        uploaded = self.client.put(
            "/api/workspace/upload",
            params={"directory": str(folder), "filename": "tema.txt"},
            content=b"BIO-U01",
        )
        self.assertEqual(uploaded.status_code, 200)
        listing = self.client.get("/api/workspace/files", params={"path": str(folder)})
        self.assertEqual([item["name"] for item in listing.json()["entries"]], ["tema.txt"])
        download = self.client.get("/api/workspace/download", params={"path": str(folder / "tema.txt")})
        self.assertEqual(download.content, b"BIO-U01")
        repeated = self.client.put(
            "/api/workspace/upload",
            params={"directory": str(folder), "filename": "tema.txt"},
            content=b"replacement",
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual((folder / "tema.txt").read_bytes(), b"BIO-U01")

    def test_rejects_paths_and_symlinks_outside_configured_root(self) -> None:
        (self.root / "escape").symlink_to(self.outside)
        listing = self.client.get("/api/workspace/files", params={"path": str(self.root)})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["entries"], [])
        self.assertEqual(self.client.get("/api/workspace/download", params={"path": str(self.outside)}).status_code, 403)
        self.assertEqual(self.client.get("/api/workspace/download", params={"path": str(self.root / "escape")}).status_code, 403)
        self.assertEqual(self.client.put(
            "/api/workspace/upload", params={"directory": str(self.root), "filename": "../escape"}, content=b"bad"
        ).status_code, 400)

    @patch("workspace_api.shutil.which", return_value="/usr/local/bin/opencode")
    def test_opencode_command_keeps_prompt_as_one_argument(self, _which: object) -> None:
        run = OpenCodeRun("run-1", str(self.root), "--help; echo ignored", "openrouter/example/model", "ses_12345")
        command = command_for_run(run, [str(self.root / "tema.txt")], False)
        self.assertEqual(command[-2:], ["--", "--help; echo ignored"])
        self.assertIn("--session", command)
        self.assertNotIn("--auto", command)
        command_with_actions = command_for_run(run, [], True)
        self.assertIn("--auto", command_with_actions)

    def test_json_events_capture_session_and_text(self) -> None:
        run = OpenCodeRun("run-1", str(self.root), "hola", "openrouter/example/model", None)
        absorb_event(run, b'{"type":"text","sessionID":"ses_12345","part":{"type":"text","text":"Respuesta"}}')
        self.assertEqual(run.session_id, "ses_12345")
        self.assertEqual(run.output, "Respuesta")

    def test_step_finish_counts_tokens_and_provider_cost(self) -> None:
        run = OpenCodeRun("run-1", str(self.root), "hola", "openrouter/example/model", None)
        absorb_event(run, json.dumps({"type": "step_finish", "part": {
            "type": "step-finish", "tokens": {"total": 140, "input": 80, "output": 20,
                "reasoning": 5, "cache": {"read": 40, "write": 0}}, "cost": 0.012,
        }}).encode())
        self.assertEqual(run.usage["total"], 140)
        self.assertEqual(run.usage["cache_read"], 40)
        self.assertEqual(run.usage["cost_usd"], 0.012)

    def test_export_recovers_final_step_missing_from_stream(self) -> None:
        run = OpenCodeRun("run-1", str(self.root), "hola", "openrouter/example/model", "ses_12345")
        absorb_event(run, json.dumps({"type": "step_finish", "part": {
            "type": "step-finish", "tokens": {"total": 100, "input": 80, "output": 20}, "cost": 0.01,
        }}).encode())
        saved = {"messages": [{"info": {"role": "assistant", "time": {"created": run.started_at_ms}},
            "parts": [
                {"type": "step-finish", "tokens": {"total": 100, "input": 80, "output": 20}, "cost": 0.01},
                {"type": "step-finish", "tokens": {"total": 50, "input": 30, "output": 20}, "cost": 0.02},
            ]}]}
        fake = Path(self.tempdir.name) / "fake-export"
        fake.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps(" + repr(saved) + "))\n", encoding="utf-8")
        fake.chmod(0o755)
        with patch("workspace_api.shutil.which", return_value=str(fake)):
            asyncio.run(reconcile_usage(run))
        self.assertEqual(run.usage["total"], 150)
        self.assertAlmostEqual(run.usage["cost_usd"], 0.03)

    def test_exchange_rate_reads_ecb_usd_reference(self) -> None:
        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url):
                xml = b"<Envelope><Cube><Cube time='2026-09-25'><Cube currency='USD' rate='1.1403'/></Cube></Cube></Envelope>"
                return httpx.Response(200, content=xml, request=httpx.Request("GET", url))

        with patch("workspace_api.httpx.AsyncClient", return_value=FakeClient()):
            response = self.client.get("/api/workspace/exchange-rate")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"usd_per_eur": 1.1403, "date": "2026-09-25"})

    def test_unchecked_prompt_denies_changes_and_commands(self) -> None:
        environment = environment_for_run(False)
        policy = json.loads(environment["OPENCODE_CONFIG_CONTENT"])["permission"]
        self.assertEqual(policy["*"], "deny")
        self.assertEqual(policy["read"]["*"], "allow")
        self.assertEqual(policy["read"]["*.env"], "deny")
        self.assertEqual(
            environment_for_run(True).get("OPENCODE_CONFIG_CONTENT"),
            os.environ.get("OPENCODE_CONFIG_CONTENT"),
        )

    def test_prompt_uses_full_tools_unless_read_only_is_requested(self) -> None:
        with patch("workspace_api.shutil.which", return_value="/usr/local/bin/opencode"):
            with patch("workspace_api.execute_run", new_callable=AsyncMock) as execute:
                started = self.client.post("/api/workspace/runs", json={
                    "prompt": "Crea un PDF", "model": "openrouter/example/model", "directory": str(self.root),
                })
                self.assertEqual(started.status_code, 200)
                self.assertTrue(execute.call_args.args[2])
                RUNS.clear()
                read_only = self.client.post("/api/workspace/runs", json={
                    "prompt": "Lee este PDF", "model": "openrouter/example/model", "directory": str(self.root),
                    "allow_actions": False,
                })
                self.assertEqual(read_only.status_code, 200)
                self.assertFalse(execute.call_args.args[2])

    def test_prompt_runs_opencode_and_returns_text_and_session(self) -> None:
        fake = Path(self.tempdir.name) / "fake-opencode"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "print('{\"type\":\"text\",\"sessionID\":\"ses_12345\",\"part\":{\"type\":\"text\",\"text\":\"Hecho\"}}', flush=True)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        with patch("workspace_api.shutil.which", return_value=str(fake)):
            with TestClient(self.app) as client:
                started = client.post("/api/workspace/runs", json={
                    "prompt": "Resume los archivos", "model": "openrouter/example/model",
                    "directory": str(self.root), "allow_actions": False,
                })
                self.assertEqual(started.status_code, 200)
                run_id = started.json()["id"]
                for _ in range(30):
                    status = client.get(f"/api/workspace/runs/{run_id}").json()
                    if status["status"] != "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(status["status"], "done")
                self.assertEqual(status["output"], "Hecho")
                self.assertEqual(status["session_id"], "ses_12345")


if __name__ == "__main__":
    unittest.main()
