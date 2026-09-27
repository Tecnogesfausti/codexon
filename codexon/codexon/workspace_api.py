"""File browser and OpenCode runs for the Codexon Ingress workspace tab."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/workspace", tags=["workspace"])

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_PROMPT_CHARS = 20_000
MAX_OUTPUT_CHARS = 200_000
MAX_RUNNING = 2
RUNS: dict[str, "OpenCodeRun"] = {}
MODEL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*/[a-zA-Z0-9][a-zA-Z0-9_.:/+@-]{1,160}$")
SESSION_PATTERN = re.compile(r"^ses_[a-zA-Z0-9_-]{4,120}$")


@dataclass
class OpenCodeRun:
    id: str
    directory: str
    prompt: str
    model: str
    session_id: str | None
    status: str = "running"
    output: str = ""
    error: str = ""
    activity: str = "Iniciando OpenCode…"
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "directory": self.directory,
            "prompt": self.prompt,
            "model": self.model,
            "session_id": self.session_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "activity": self.activity,
        }


def workspace_roots() -> list[Path]:
    configured = os.getenv("CODEXON_FS_ROOTS", "/ha_config,/addon_config,/share")
    roots: list[Path] = []
    for item in configured.split(","):
        raw = item.strip()
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def confined_path(raw: str | None, *, existing: bool = True) -> tuple[Path, Path]:
    roots = workspace_roots()
    if not roots:
        raise HTTPException(status_code=503, detail="No hay carpetas de trabajo disponibles")
    requested = Path(raw).expanduser() if raw else roots[0]
    if not requested.is_absolute():
        raise HTTPException(status_code=400, detail="La ruta debe ser absoluta")
    resolved = requested.resolve(strict=False)
    root = next((item for item in roots if resolved.is_relative_to(item)), None)
    if root is None:
        raise HTTPException(status_code=403, detail="Ruta fuera de las carpetas permitidas")
    if existing and not resolved.exists():
        raise HTTPException(status_code=404, detail="Archivo o carpeta no encontrado")
    return resolved, root


def safe_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail="Nombre de archivo o carpeta inválido")
    return name


@router.get("/roots")
def api_roots() -> dict[str, Any]:
    roots = workspace_roots()
    return {
        "roots": [{"name": str(root), "path": str(root)} for root in roots],
        "default": str(roots[0]) if roots else None,
        "opencode_available": shutil.which("opencode") is not None,
    }


@router.get("/files")
def api_files(path: str | None = None) -> dict[str, Any]:
    directory, root = confined_path(path)
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="La ruta no es una carpeta")
    rows: list[dict[str, Any]] = []
    try:
        for entry in directory.iterdir():
            # Do not expose symlinks that leave the configured root.
            target = entry.resolve(strict=False)
            if not target.is_relative_to(root):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            if entry.is_dir():
                kind = "directory"
            elif entry.is_file():
                kind = "file"
            else:
                continue
            rows.append({
                "name": entry.name,
                "path": str(entry),
                "kind": kind,
                "bytes": stat.st_size if kind == "file" else None,
            })
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="No se puede leer esta carpeta") from exc
    rows.sort(key=lambda row: (row["kind"] != "directory", row["name"].casefold()))
    return {
        "path": str(directory),
        "root": str(root),
        "parent": str(directory.parent) if directory != root and directory.parent.is_relative_to(root) else None,
        "entries": rows,
    }


@router.get("/download")
def api_download(path: str) -> FileResponse:
    file_path, _ = confined_path(path)
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="La ruta no es un archivo")
    return FileResponse(file_path, filename=file_path.name, media_type="application/octet-stream")


@router.put("/upload")
async def api_upload(request: Request, directory: str, filename: str, overwrite: bool = False) -> dict[str, Any]:
    folder, root = confined_path(directory)
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail="El destino no es una carpeta")
    target = folder / safe_name(filename)
    if not target.resolve(strict=False).is_relative_to(root):
        raise HTTPException(status_code=403, detail="Destino fuera de la carpeta permitida")
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=409, detail="Ya existe una carpeta con ese nombre")
    if not overwrite and (target.exists() or target.is_symlink()):
        raise HTTPException(status_code=409, detail="El archivo ya existe")
    temporary = folder / f".codexon-upload-{uuid.uuid4().hex}.tmp"
    size = 0
    try:
        with temporary.open("xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="El archivo supera los 100 MB")
                await asyncio.to_thread(stream.write, chunk)
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)  # Fails safely if the name appeared during upload.
            temporary.unlink()
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="El archivo ya existe") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="No se puede escribir en esta carpeta") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "path": str(target), "bytes": size}


@router.post("/folders")
def api_create_folder(payload: dict[str, Any]) -> dict[str, Any]:
    folder, root = confined_path(str(payload.get("directory") or ""))
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail="El destino no es una carpeta")
    target = folder / safe_name(str(payload.get("name") or ""))
    if not target.resolve(strict=False).is_relative_to(root):
        raise HTTPException(status_code=403, detail="Destino fuera de la carpeta permitida")
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="Ya existe un elemento con ese nombre") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="No se puede crear la carpeta") from exc
    return {"ok": True, "path": str(target)}


@router.get("/models")
async def api_models() -> dict[str, Any]:
    binary = shutil.which("opencode")
    if binary is None:
        raise HTTPException(status_code=503, detail="OpenCode no está instalado")
    process = await asyncio.create_subprocess_exec(
        binary, "models", "openrouter",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="OpenCode tardó demasiado en listar modelos") from exc
    if process.returncode:
        raise HTTPException(status_code=502, detail=stderr.decode("utf-8", "replace")[-500:] or "No se pudo consultar el catálogo")
    models = [line.strip() for line in stdout.decode("utf-8", "replace").splitlines() if line.strip().startswith("openrouter/")]
    return {"models": models[:1000]}


def command_for_run(run: OpenCodeRun, files: list[str], allow_actions: bool) -> list[str]:
    binary = shutil.which("opencode")
    if binary is None:
        raise HTTPException(status_code=503, detail="OpenCode no está instalado")
    command = [binary, "run", "--format", "json", "--dir", run.directory, "--model", run.model]
    if run.session_id:
        command.extend(["--session", run.session_id])
    if allow_actions:
        command.append("--auto")
    for file_path in files:
        command.extend(["--file", file_path])
    command.extend(["--", run.prompt])
    return command


def environment_for_run(allow_actions: bool) -> dict[str, str]:
    environment = os.environ.copy()
    if allow_actions:
        return environment
    # OpenCode defaults to allowing edits and shell commands. Inline config has
    # precedence over project config, so this request stays read-only.
    try:
        existing = json.loads(environment.get("OPENCODE_CONFIG_CONTENT") or "{}")
    except json.JSONDecodeError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["permission"] = {
        "*": "deny",
        "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
        "list": "allow",
        "glob": "allow",
        "grep": "allow",
        "lsp": "allow",
        "webfetch": "allow",
        "websearch": "allow",
    }
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(existing)
    return environment


def absorb_event(run: OpenCodeRun, line: bytes) -> None:
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(event, dict):
        return
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    session_id = event.get("sessionID") or part.get("sessionID")
    if isinstance(session_id, str) and SESSION_PATTERN.fullmatch(session_id):
        run.session_id = session_id
    event_type = str(event.get("type") or "")
    if part.get("type") == "text" or event_type == "text":
        chunk = part.get("text") or event.get("text")
        if isinstance(chunk, str):
            run.output = (run.output + chunk)[-MAX_OUTPUT_CHARS:]
            run.activity = "Respondiendo…"
    elif part.get("type") == "tool":
        tool = str(part.get("tool") or "herramienta")[:80]
        run.activity = f"Ejecutando {tool}…"
    elif event_type == "error":
        error = event.get("error")
        run.error = str(error)[-4000:]


async def execute_run(run: OpenCodeRun, files: list[str], allow_actions: bool) -> None:
    if run.status == "cancelled":
        return
    stderr_task: asyncio.Task[str] | None = None
    try:
        command = command_for_run(run, files, allow_actions)
        process = await asyncio.create_subprocess_exec(
            *command, cwd=run.directory, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=1024 * 1024,
            env=environment_for_run(allow_actions),
        )
        run.process = process
        if run.status == "cancelled":
            os.killpg(process.pid, signal.SIGTERM)

        async def read_stderr() -> str:
            assert process.stderr is not None
            chunks: list[bytes] = []
            while chunk := await process.stderr.read(4096):
                chunks.append(chunk)
                if len(chunks) > 30:
                    chunks.pop(0)
            return b"".join(chunks).decode("utf-8", "replace")[-4000:]

        stderr_task = asyncio.create_task(read_stderr())
        assert process.stdout is not None
        try:
            async with asyncio.timeout(900):
                while line := await process.stdout.readline():
                    absorb_event(run, line)
                await process.wait()
                stderr = await stderr_task
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            await process.wait()
            run.error = "OpenCode superó el límite de 15 minutos"
            stderr = await stderr_task
        if run.status == "cancelled":
            return
        if process.returncode == 0:
            run.status = "done"
            run.activity = "Terminado"
        else:
            run.status = "error"
            run.error = run.error or stderr or "OpenCode terminó con un error"
            run.activity = "Error"
    except asyncio.CancelledError:
        process = run.process
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            await process.wait()
        run.status = "cancelled"
        run.activity = "Cancelado"
        raise
    except Exception as exc:
        process = run.process
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            await process.wait()
        run.status = "error"
        run.error = str(exc)[-4000:]
        run.activity = "Error"
    finally:
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
        run.process = None


@router.post("/runs")
async def api_start_run(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=400, detail="El prompt debe tener entre 1 y 20.000 caracteres")
    model = str(payload.get("model") or "").strip()
    if not MODEL_PATTERN.fullmatch(model):
        raise HTTPException(status_code=400, detail="Elige un modelo en formato proveedor/modelo")
    directory, _ = confined_path(str(payload.get("directory") or ""))
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="La carpeta de trabajo no existe")
    session_id = str(payload.get("session_id") or "").strip() or None
    if session_id and not SESSION_PATTERN.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Sesión de OpenCode inválida")
    if len([run for run in RUNS.values() if run.status == "running"]) >= MAX_RUNNING:
        raise HTTPException(status_code=429, detail="Ya hay dos tareas de OpenCode en marcha")
    if session_id and any(run.status == "running" and run.session_id == session_id for run in RUNS.values()):
        raise HTTPException(status_code=409, detail="Esta conversación ya tiene una tarea en marcha")
    raw_files = payload.get("files") or []
    if not isinstance(raw_files, list) or len(raw_files) > 10:
        raise HTTPException(status_code=400, detail="Puedes adjuntar hasta 10 archivos")
    files: list[str] = []
    for raw_file in raw_files:
        path, _ = confined_path(str(raw_file))
        if not path.is_file():
            raise HTTPException(status_code=400, detail="El adjunto no es un archivo")
        files.append(str(path))
    if shutil.which("opencode") is None:
        raise HTTPException(status_code=503, detail="OpenCode no está instalado")
    run = OpenCodeRun(
        id=uuid.uuid4().hex, directory=str(directory), prompt=prompt,
        model=model, session_id=session_id,
    )
    for old_id, old in list(RUNS.items()):
        if len(RUNS) <= 50:
            break
        if old.status != "running":
            RUNS.pop(old_id, None)
    RUNS[run.id] = run
    asyncio.create_task(execute_run(run, files, payload.get("allow_actions") is True))
    return run.public()


@router.get("/runs/{run_id}")
def api_run_status(run_id: str) -> dict[str, Any]:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Ejecución no encontrada")
    return run.public()


@router.delete("/runs/{run_id}")
def api_cancel_run(run_id: str) -> dict[str, Any]:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Ejecución no encontrada")
    if run.status == "running":
        run.status = "cancelled"
        run.activity = "Cancelado"
        process = run.process
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
    return run.public()
