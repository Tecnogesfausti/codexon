import asyncio
import base64
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import codexon_web
from services import chatgpt_image_analysis as vision


def events(*items):
    return b"\n".join(json.dumps(item).encode() for item in items)


ANSWER = events(
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Un círculo azul."}},
    {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 25, "reasoning_output_tokens": 10}},
)


class ChatGPTImageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = {"models": [{"id": "gpt-6-astra", "name": "Astra", "reasoning_effort": "low"},
                                 {"id": "gpt-6-luna", "name": "Luna", "reasoning_effort": "low"}],
                      "default_model": "gpt-6-astra", "quota": {"windows": []}, "quota_error": None}
        reader = patch.object(vision, "read_subscription_state", AsyncMock(return_value=self.state))
        self.reader = reader.start()
        self.addCleanup(reader.stop)

    async def test_subscription_auth_image_and_cleanup(self):
        image_paths = []

        async def run(args, **kwargs):
            if args[1:3] == ["login", "status"]:
                return 0, b"Logged in using ChatGPT"
            image = Path(args[args.index("--image") + 1])
            image_paths.append(image)
            self.assertEqual(image.read_bytes(), b"png")
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertIn("--ignore-user-config", args)
            self.assertIn("features.shell_tool=false", args)
            self.assertIn("features.apps=false", args)
            self.assertIn("features.multi_agent=false", args)
            self.assertIn("¿Qué ves?".encode(), kwargs["prompt"])
            return 0, ANSWER

        with patch.object(vision, "_run", side_effect=run), patch.object(vision.shutil, "which", return_value="codex"), patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            result = await vision.analyze_image_with_chatgpt(image_bytes=b"png", media_type="image/png", question="¿Qué ves?")
        self.assertEqual(result["answer"], "Un círculo azul.")
        self.assertEqual(result["provider"], "chatgpt")
        self.assertEqual(result["usage"]["total_tokens"], 125)
        self.assertEqual(result["usage"]["cached_input_tokens"], 60)
        self.assertFalse(image_paths[0].exists())

    async def test_api_key_login_is_rejected_before_inference(self):
        with patch.object(vision, "_run", AsyncMock(return_value=(0, b"Logged in using an API key"))) as run, patch.object(vision.shutil, "which", return_value="codex"):
            with self.assertRaisesRegex(RuntimeError, "Inicia sesión con ChatGPT"):
                await vision.analyze_image_with_chatgpt(image_bytes=b"png", media_type="image/png", question="Describe")
            self.assertEqual(run.await_count, 1)

    async def test_timeout_removes_uploaded_image(self):
        paths = []

        async def run(args, **kwargs):
            if args[1] == "login":
                return 0, b"Logged in using ChatGPT"
            paths.append(Path(args[args.index("--image") + 1]))
            raise asyncio.TimeoutError

        with patch.object(vision, "_run", side_effect=run), patch.object(vision.shutil, "which", return_value="codex"):
            with self.assertRaisesRegex(RuntimeError, "tardado demasiado"):
                await vision.analyze_image_with_chatgpt(image_bytes=b"png", media_type="image/png", question="Describe")
        self.assertFalse(paths[0].exists())

    def test_partial_or_failed_answers_are_not_success(self):
        self.assertEqual(vision.completed_answer(ANSWER), "Un círculo azul.")
        for output in (ANSWER.splitlines()[0], ANSWER + b'\n{"type":"turn.failed"}', b'{}'):
            with self.assertRaises(RuntimeError):
                vision.completed_answer(output)

    async def test_endpoint_chatgpt_does_not_use_openrouter(self):
        payload = {"provider": "chatgpt", "question": "Describe", "media_type": "image/png", "image_base64": base64.b64encode(b"png").decode(), "model": "gpt-6-luna"}
        with patch.object(codexon_web, "analyze_image_with_chatgpt", AsyncMock(return_value={"answer": "Prueba", "model": "test", "provider": "chatgpt"})) as analyze, patch.object(codexon_web, "build_model_router", AsyncMock()) as router, patch.object(codexon_web, "analyze_image", AsyncMock()) as openrouter:
            result = await codexon_web.api_image_analysis(payload)
            self.assertTrue(result["ok"])
            analyze.assert_awaited_once()
            self.assertEqual(analyze.await_args.kwargs["model"], "gpt-6-luna")
            router.assert_not_awaited()
            openrouter.assert_not_awaited()
            analyze.side_effect = RuntimeError("Sin sesión")
            with self.assertRaises(HTTPException) as error:
                await codexon_web.api_image_analysis(payload)
            self.assertEqual(error.exception.status_code, 502)
            openrouter.assert_not_awaited()

    async def test_process_timeout_kills_and_reaps(self):
        process = AsyncMock()
        process.pid = 12345
        process.communicate.side_effect = [asyncio.TimeoutError, (b"", b"")]
        with patch.object(vision.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), patch.object(vision.os, "killpg") as kill:
            with self.assertRaises(asyncio.TimeoutError):
                await vision._run(["codex"], env={}, timeout=1)
            kill.assert_called_once_with(process.pid, vision.signal.SIGKILL)
            self.assertEqual(process.communicate.await_count, 2)

    async def test_selected_model_is_used_and_unknown_model_rejected(self):
        run = AsyncMock(side_effect=[(0, b"Logged in using ChatGPT"), (0, ANSWER)])
        with patch.object(vision, "_run", run), patch.object(vision.shutil, "which", return_value="codex"):
            result = await vision.analyze_image_with_chatgpt(image_bytes=b"png", media_type="image/png", question="Describe", model="gpt-6-luna")
            self.assertEqual(result["model"], "gpt-6-luna")
            args = run.await_args_list[1].args[0]
            self.assertEqual(args[args.index("--model") + 1], "gpt-6-luna")
            run.reset_mock(side_effect=True)
            run.return_value = (0, b"Logged in using ChatGPT")
            with self.assertRaisesRegex(ValueError, "modelo con visión disponible"):
                await vision.analyze_image_with_chatgpt(image_bytes=b"png", media_type="image/png", question="Describe", model="unavailable")
            self.assertEqual(run.await_count, 1)

    async def test_after_quota_failure_preserves_answer_and_tokens(self):
        self.reader.side_effect = [self.state, RuntimeError("unavailable")]
        with patch.object(vision, "_run", AsyncMock(side_effect=[(0, b"Logged in using ChatGPT"), (0, ANSWER)])), patch.object(vision.shutil, "which", return_value="codex"):
            result = await vision.analyze_image_with_chatgpt(image_bytes=b"png", media_type="image/png", question="Describe")
        self.assertEqual(result["answer"], "Un círculo azul.")
        self.assertEqual(result["usage"]["total_tokens"], 125)
        self.assertIsNone(result["quota_after"])
        self.assertTrue(result["quota_error"])

    def test_usage_missing_is_not_reported_as_zero(self):
        self.assertIsNone(vision.completed_usage(b'{"type":"turn.completed"}'))
        self.assertIsNone(vision.completed_usage(b'invalid json'))
