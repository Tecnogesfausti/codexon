"""Read model availability and quota through Codex's public app-server protocol."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import shutil
import signal
import time


def subscription_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        env.pop(name, None)
    return env


@asynccontextmanager
async def _rpc_session():
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("Codex no está instalado en este complemento")
    process = await asyncio.create_subprocess_exec(
        executable, "app-server", "-c", "features.apps=false", "-c", "features.plugins=false",
        env=subscription_environment(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True, limit=2 * 1024 * 1024,
    )
    request_id = 0

    async def request(method: str, params: dict | None = None):
        nonlocal request_id
        request_id += 1
        current_id = request_id
        process.stdin.write((json.dumps({"id": current_id, "method": method, "params": params or {}}) + "\n").encode())
        await process.stdin.drain()
        while True:
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("No se pudo consultar la sesión de Codex")
            message = json.loads(line)
            if message.get("id") != current_id:
                continue
            if "error" in message:
                raise RuntimeError("Codex no pudo consultar los modelos o el uso de la suscripción")
            return message.get("result") or {}

    try:
        await asyncio.wait_for(request("initialize", {"clientInfo": {"name": "codexon_image_analysis", "version": "1.0.0"}}), timeout=15)
        process.stdin.write(b'{"method":"initialized"}\n')
        await process.stdin.drain()
        yield request
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()


def normalize_quota(result: dict) -> dict:
    """Keep usage windows only; do not expose account IDs or redeemable credits."""
    buckets = result.get("rateLimitsByLimitId") or {}
    if not buckets and result.get("rateLimits"):
        single = result["rateLimits"]
        buckets = {single.get("limitId") or "codex": single}
    windows = []
    for bucket_id, bucket in buckets.items():
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if not window or window.get("usedPercent") is None:
                continue
            windows.append({
                "bucket": str(bucket_id), "bucket_name": bucket.get("limitName") or str(bucket_id),
                "window": key, "used_percent": window["usedPercent"],
                "duration_minutes": window.get("windowDurationMins"), "resets_at": window.get("resetsAt"),
            })
    return {"observed_at": time.time(), "windows": windows}


async def read_subscription_state(*, include_models: bool = False) -> dict:
    state = {"quota": None, "quota_error": None}
    try:
        async with asyncio.timeout(40):
            async with _rpc_session() as request:
                if include_models:
                    models = []
                    cursor = None
                    for _ in range(10):
                        params = {"limit": 100, "includeHidden": False}
                        if cursor:
                            params["cursor"] = cursor
                        page = await request("model/list", params)
                        for item in page.get("data", []):
                            if item.get("hidden") or "image" not in (item.get("inputModalities") or ["text", "image"]):
                                continue
                            efforts = [option.get("reasoningEffort") for option in item.get("supportedReasoningEfforts", [])]
                            models.append({
                                "id": item.get("model") or item["id"],
                                "name": item.get("displayName") or item["id"],
                                "reasoning_effort": "low" if "low" in efforts else item.get("defaultReasoningEffort", "low"),
                                "is_default": bool(item.get("isDefault")),
                            })
                        cursor = page.get("nextCursor")
                        if not cursor:
                            break
                    if not models:
                        raise RuntimeError("No hay modelos con visión disponibles en tu sesión de ChatGPT")
                    configured = os.getenv("CODEXON_CHATGPT_IMAGE_MODEL", "gpt-6-astra").strip()
                    default = next((m["id"] for m in models if m["id"] == configured), None)
                    state.update(models=models, default_model=default or next((m["id"] for m in models if m["is_default"]), models[0]["id"]))
                try:
                    state["quota"] = normalize_quota(await request("account/rateLimits/read"))
                except (RuntimeError, ValueError, OSError):
                    state["quota_error"] = "El porcentaje de uso no está disponible en este momento."
    except asyncio.TimeoutError:
        if include_models and "models" not in state:
            raise RuntimeError("La consulta de modelos de ChatGPT ha tardado demasiado") from None
        state["quota_error"] = "La consulta del porcentaje de uso ha tardado demasiado."
    return state


def quota_changes(before: dict | None, after: dict | None) -> list[dict]:
    prior = {(w["bucket"], w["window"]): w for w in (before or {}).get("windows", [])}
    changes = []
    for end in (after or {}).get("windows", []):
        start = prior.get((end["bucket"], end["window"]))
        if start is None:
            continue
        same_window = start.get("resets_at") is not None and start.get("resets_at") == end.get("resets_at") and start.get("duration_minutes") == end.get("duration_minutes")
        difference = end["used_percent"] - start["used_percent"]
        changes.append({
            "bucket": end["bucket"], "bucket_name": end["bucket_name"], "window": end["window"],
            "duration_minutes": end["duration_minutes"], "before_percent": start["used_percent"],
            "after_percent": end["used_percent"],
            "delta_percentage_points": round(difference, 4) if same_window and difference >= 0 else None,
            "reset_or_changed": not same_window or difference < 0,
        })
    return changes
