"""Analisis multimodal compartido por el portal y WhatsApp."""

from __future__ import annotations

import base64
import os
from typing import Any


MAX_IMAGE_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
SAFETY_ONLY_LINES = {
    "user safety: safe", "response safety: safe",
    "user safety: unsafe", "response safety: unsafe",
}


def validate_image(*, image_bytes: bytes, media_type: str) -> None:
    if media_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Formato no admitido; usa JPEG, PNG, WebP o GIF")
    if not image_bytes:
        raise ValueError("La imagen esta vacia")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("La imagen supera el limite de 12 MB")


async def analyze_image(
    *,
    image_bytes: bytes,
    media_type: str,
    question: str,
    client: Any | None = None,
    model: str | None = None,
    fallback_models: tuple[str, ...] = (),
) -> dict[str, str]:
    """Pregunta por una imagen usando el enrutador multimodal de OpenRouter."""
    validate_image(image_bytes=image_bytes, media_type=media_type)
    prompt = question.strip()
    if not prompt:
        raise ValueError("Escribe una pregunta acerca de la foto")
    if len(prompt) > 4000:
        raise ValueError("La pregunta supera los 4000 caracteres")

    if client is None:
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Falta configurar openrouter_api_key")
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

    configured_model = os.getenv("CODEXON_IMAGE_MODEL", "").strip()
    selected_model = str(model or configured_model or "openrouter/auto").strip()
    data_url = (
        f"data:{media_type};base64,"
        + base64.b64encode(image_bytes).decode("ascii")
    )
    messages = [
            {
                "role": "system",
                "content": (
                    "Analiza la imagen con cuidado y responde en espanol. "
                    "Distingue hechos visibles de inferencias y reconoce cuando un detalle no es legible."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
    attempted: list[str] = []
    last_problem = ""
    for candidate in (selected_model, *fallback_models):
        candidate = str(candidate or "").strip()
        if not candidate or candidate in attempted:
            continue
        attempted.append(candidate)
        response = await client.chat.completions.create(
            model=candidate, messages=messages, temperature=0.2
        )
        answer = str(response.choices[0].message.content or "").strip()
        if not answer:
            last_problem = f"{candidate} no devolvio una respuesta"
            continue
        normalized_lines = {
            line.strip().casefold() for line in answer.splitlines() if line.strip()
        }
        if normalized_lines and normalized_lines <= SAFETY_ONLY_LINES:
            last_problem = f"{candidate} devolvio solo una clasificacion de seguridad"
            continue
        used_model = str(getattr(response, "model", "") or candidate)
        return {"answer": answer, "model": used_model}
    raise RuntimeError(last_problem or "Ningun modelo visual devolvio una respuesta util")
