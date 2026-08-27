from __future__ import annotations

import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import codexon_web
from services.image_analysis import analyze_image, validate_image


class ImageAnalysisTest(unittest.IsolatedAsyncioTestCase):
    async def test_multimodal_request_contains_question_and_data_url(self) -> None:
        create = AsyncMock(
            return_value=SimpleNamespace(
                model="vision/test",
                choices=[SimpleNamespace(message=SimpleNamespace(content="Veo una planta."))],
            )
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        result = await analyze_image(
            image_bytes=b"jpeg-data",
            media_type="image/jpeg",
            question="¿Qué ves?",
            client=client,
            model="vision/selected",
        )

        self.assertEqual(result, {"answer": "Veo una planta.", "model": "vision/test"})
        request = create.await_args.kwargs
        self.assertEqual(request["model"], "vision/selected")
        content = request["messages"][1]["content"]
        self.assertEqual(content[0]["text"], "¿Qué ves?")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_rejects_invalid_type_and_empty_image(self) -> None:
        with self.assertRaises(ValueError):
            validate_image(image_bytes=b"x", media_type="application/pdf")
        with self.assertRaises(ValueError):
            validate_image(image_bytes=b"", media_type="image/png")

    async def test_web_endpoint_returns_answer_in_same_request(self) -> None:
        router = SimpleNamespace(config={"routes": {"image_analysis": {"model": "vision/default"}}})
        with (
            patch.object(codexon_web, "get_setting", return_value="vision/selected"),
            patch.object(codexon_web, "build_model_router", AsyncMock(return_value=router)),
            patch.object(
                codexon_web,
                "analyze_image",
                AsyncMock(return_value={"answer": "Un contador", "model": "vision/selected"}),
            ) as analyzer,
        ):
            result = await codexon_web.api_image_analysis(
                {
                    "question": "¿Qué es?",
                    "media_type": "image/png",
                    "image_base64": base64.b64encode(b"png").decode(),
                }
            )
        self.assertEqual(result["answer"], "Un contador")
        self.assertEqual(analyzer.await_args.kwargs["model"], "vision/selected")

    async def test_web_endpoint_rejects_bad_base64(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await codexon_web.api_image_analysis(
                {"question": "¿Qué es?", "media_type": "image/png", "image_base64": "!!!"}
            )
        self.assertEqual(raised.exception.status_code, 400)
