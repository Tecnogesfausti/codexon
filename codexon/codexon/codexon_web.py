from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import re
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from agents.manager import AgentManager
from event_engine.storage import (
    create_subscription,
    ensure_event_schema,
    set_subscription_enabled,
)
from scheduler.metrics import scheduler_monitor
from services.live_context.manager import LiveContextManager
from tools.registry import builtin_tool_names
from codexon import DEFAULT_MODEL_ROUTES, ModelRouter, fetch_openrouter_model_catalog

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

APP_NAME = "Codexon"
DEFAULT_TASK_MAX_ATTEMPTS = 3
DEFAULT_TASK_RETRY_BACKOFF_SECONDS = 30
MAX_TASK_INTERVAL_SECONDS = 315_360_000
VALID_TASK_STATUSES = {"pending", "cancelled"}


def default_data_dir() -> Path:
    configured = os.getenv("CODEXON_DATA_DIR")
    if configured:
        return Path(configured)
    if os.getenv("SUPERVISOR_TOKEN"):
        return Path("/data/codexon")
    return Path("data")


DATA_DIR = default_data_dir()
DB_PATH = Path(os.getenv("CODEXON_DB", str(DATA_DIR / "codexon_memory.sqlite3")))
LOG_PATH = Path(os.getenv("CODEXON_LOG_FILE", str(DATA_DIR / "codexon_runtime.log")))
AGENTS_DIR = Path(os.getenv("CODEXON_AGENTS_DIR", "agents"))
AGENT_CONFIG_PATH = Path(os.getenv("CODEXON_AGENT_CONFIG", str(DATA_DIR / "agent_config.json")))
CODEX_CONTEXT_PATH = Path(os.getenv("CODEXON_CODEX_CONTEXT", str(DATA_DIR / "CODEX_CONTEXT.md")))
CODEX_NOTES_PATH = Path(os.getenv("CODEXON_CODEX_NOTES", str(DATA_DIR / "CODEX_NOTES.md")))
BACKUP_DIR = Path(os.getenv("CODEXON_BACKUP_DIR", str(DATA_DIR / "backups")))
WHATSAPP_DATA_DIR = Path(
    os.getenv("CODEXON_WHATSAPP_DATA_DIR", str(DATA_DIR / "whatsapp"))
)
BACKUP_KEY = os.getenv("CODEXON_BACKUP_KEY", "")
MODEL_PAGE_SIZE = 50
MODEL_CATALOG_CACHE: dict[str, dict[str, Any]] = {}

app = FastAPI(title="Codexon", version="0.3.4")


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def normalize_cancellation_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    folded = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", folded.lower()).strip()


def task_recovery_policy(instruction: str) -> str:
    lowered = normalize_cancellation_key(instruction)
    markers = (
        "automation_plan_v1", "deterministic_water_liters", "button.", "ha_press_entity_interval", "service=press", '"service":"press"',
        "toggle", "pulsa", "pulsar", "sirena", "fireworks", "notify/",
        "ha_send_mobile_alert", "google_translate_say", "rtttl", "tono", "por voz",
        "repetir", "veces", "ha_call_service", "turn_on", "turn_off",
        "enciende", "encender", "apaga", "apagar", "activa", "activar",
        "desactiva", "desactivar", "abre", "abrir", "cierra", "cerrar",
    )
    return "manual" if any(marker in lowered for marker in markers) else "retry"


def parse_interval(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        interval = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="interval_seconds debe ser entero") from exc
    if not 1 <= interval <= MAX_TASK_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"interval_seconds debe estar entre 1 y {MAX_TASK_INTERVAL_SECONDS}",
        )
    return interval


def bounded_int(value: Any, *, field: str, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} debe ser entero") from exc
    if not minimum <= parsed <= maximum:
        raise HTTPException(status_code=400, detail=f"{field} debe estar entre {minimum} y {maximum}")
    return parsed


def parse_run_at(value: str | None) -> str:
    if not value:
        return utc_now()
    text = value.strip()
    if not text:
        return utc_now()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="run_at debe ser ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            topic TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.7,
            source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            summary TEXT NOT NULL,
            raw TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            estimated_cost_usd REAL,
            context TEXT,
            provider TEXT,
            duration_ms INTEGER,
            router_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            run_at TEXT NOT NULL,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            instruction TEXT NOT NULL,
            result TEXT,
            last_error TEXT,
            priority INTEGER NOT NULL DEFAULT 50,
            interval_seconds INTEGER,
            cancellation_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            retry_backoff_seconds INTEGER NOT NULL DEFAULT 30,
            lease_owner TEXT,
            lease_expires_at TEXT,
            last_started_at TEXT,
            recovery_policy TEXT NOT NULL DEFAULT 'retry'
        );
        CREATE TABLE IF NOT EXISTS task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            result TEXT,
            error TEXT,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    additions = {
        "priority": "INTEGER NOT NULL DEFAULT 50",
        "interval_seconds": "INTEGER",
        "cancellation_key": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": "INTEGER NOT NULL DEFAULT 3",
        "retry_backoff_seconds": "INTEGER NOT NULL DEFAULT 30",
        "lease_owner": "TEXT",
        "lease_expires_at": "TEXT",
        "last_started_at": "TEXT",
        "recovery_policy": "TEXT NOT NULL DEFAULT 'retry'",
    }
    for column, definition in additions.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_dispatch ON tasks(status, priority DESC, run_at ASC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_cancellation_key ON tasks(cancellation_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_task_started ON task_runs(task_id, started_at DESC)")
    for row in conn.execute("SELECT id, instruction, recovery_policy FROM tasks"):
        policy = task_recovery_policy(str(row["instruction"] or ""))
        if str(row["recovery_policy"] or "") != policy:
            conn.execute("UPDATE tasks SET recovery_policy = ? WHERE id = ?", (policy, int(row["id"])))
    ensure_event_schema(conn)
    conn.commit()


def sqlite_rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    conn = connect_db()
    try:
        return [dict(row) for row in conn.execute(query, params)]
    finally:
        conn.close()


def get_setting(key: str, default: str = "") -> str:
    if not DB_PATH.exists():
        return default
    conn = connect_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str | None) -> None:
    conn = connect_db()
    try:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )
        conn.commit()
    finally:
        conn.close()


async def build_model_router(*, refresh: bool = False) -> ModelRouter:
    global MODEL_CATALOG_CACHE
    if refresh or not MODEL_CATALOG_CACHE:
        MODEL_CATALOG_CACHE = await fetch_openrouter_model_catalog()
    return ModelRouter(Path(os.getenv("CODEXON_MODEL_ROUTES", DEFAULT_MODEL_ROUTES)), MODEL_CATALOG_CACHE)


def model_row(router: ModelRouter, model_id: str, selected_model: str | None) -> dict[str, Any]:
    meta = router.model_catalog.get(model_id) or {}
    raw_input_price = float(meta.get("input_price_per_million") or 0)
    raw_output_price = float(meta.get("output_price_per_million") or 0)
    input_price = raw_input_price if raw_input_price >= 0 else None
    output_price = raw_output_price if raw_output_price >= 0 else None
    return {
        "id": model_id,
        "name": meta.get("name") or model_id,
        "selected": model_id == selected_model,
        "configured": model_id in router.configured_models(),
        "supports_tools": bool(meta.get("supports_tools")),
        "supports_chat": bool(meta.get("supports_chat", True)),
        "supports_structured_outputs": bool(meta.get("supports_structured_outputs")),
        "context_length": meta.get("context_length"),
        "input_price_per_million": input_price,
        "output_price_per_million": output_price,
        "combined_price_per_million": (input_price + output_price) if input_price is not None and output_price is not None else None,
    }


def execute_db(query: str, params: tuple[Any, ...] = ()) -> int:
    conn = connect_db()
    try:
        cursor = conn.execute(query, params)
        conn.commit()
        return int(cursor.lastrowid or cursor.rowcount)
    finally:
        conn.close()


def usage_total() -> dict[str, Any]:
    rows = sqlite_rows(
        """
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(total_tokens), 0) AS total_tokens,
               COALESCE(SUM(CASE WHEN estimated_cost_usd > 0 THEN estimated_cost_usd ELSE 0 END), 0) AS cost
        FROM usage_events
        """
    )
    return rows[0] if rows else {"calls": 0, "total_tokens": 0, "cost": 0.0}


def tail_log(limit: int = 80) -> list[str]:
    if not LOG_PATH.exists():
        return []
    return LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]


def read_agent_config() -> dict[str, Any]:
    if not AGENT_CONFIG_PATH.exists():
        return {"agents": {}}
    try:
        data = json.loads(AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"agents": {}}
    if not isinstance(data, dict):
        return {"agents": {}}
    data.setdefault("agents", {})
    return data


def write_agent_config(data: dict[str, Any]) -> None:
    AGENT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class WebMemory:
    def add_observation(self, *, source: str, summary: str, raw: str | None = None) -> None:
        execute_db(
            "INSERT INTO observations(created_at, source, summary, raw) VALUES (?, ?, ?, ?)",
            (utc_now(), source[:80], summary.strip(), raw),
        )


@dataclasses.dataclass
class WebCodexonContext:
    ha_base_url: str | None
    ha_token: str | None
    httpx: Any
    require_action_confirmation: bool = True

    @property
    def has_homeassistant_rest(self) -> bool:
        return bool(self.ha_base_url and self.ha_token and self.httpx is not None)


def build_agent_manager() -> AgentManager:
    manager = AgentManager(
        AGENTS_DIR,
        context_services={
            "memory": WebMemory(),
            "live_context": LiveContextManager(),
            "codexon": WebCodexonContext(
                ha_base_url=os.getenv("HOME_ASSISTANT_URL") or os.getenv("HA_BASE_URL"),
                ha_token=os.getenv("HOME_ASSISTANT_TOKEN") or os.getenv("HA_TOKEN"),
                httpx=httpx,
            ),
        },
    )
    manager.discover()
    return manager


def agent_rows() -> list[dict[str, Any]]:
    manager = build_agent_manager()
    config = read_agent_config().get("agents", {})
    rows = []
    for row in manager.list_agents():
        overrides = config.get(row["name"], {}) if isinstance(config, dict) else {}
        row["enabled"] = bool(overrides.get("enabled", False))
        row["effective_priority"] = int(overrides.get("priority", row["priority"]))
        row["effective_frequency_seconds"] = int(overrides.get("frequency_seconds", row["frequency_seconds"]))
        row["overrides"] = overrides
        rows.append(row)
    return sorted(rows, key=lambda item: (-int(item["effective_priority"]), item["name"]))


def markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_Sin datos._\n"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(col, "")).replace("\n", " ")[:180] for col in columns) + " |")
    return "\n".join([header, sep, *body]) + "\n"


def build_codex_context() -> str:
    agents = agent_rows()
    tasks = api_tasks(limit=12, include_done=True)
    observations = api_observations(limit=12)
    status = api_status()
    notes = CODEX_NOTES_PATH.read_text(encoding="utf-8", errors="replace") if CODEX_NOTES_PATH.exists() else ""
    log_tail = "\n".join(tail_log(40))
    return f"""# Codexon Codex Maintenance Context

Generated: {utc_now()}

## Purpose

Use this file from an interactive Codex/tmux session to inspect, teach, correct and extend Codexon.
Codexon is the long-running service. Codex is the maintenance workshop.

## Important Paths

- App directory: `{Path.cwd()}`
- Agents directory: `{AGENTS_DIR}`
- Database: `{DB_PATH}`
- Runtime log: `{LOG_PATH}`
- Agent config: `{AGENT_CONFIG_PATH}`
- Codex context: `{CODEX_CONTEXT_PATH}`
- Codex notes: `{CODEX_NOTES_PATH}`
- Codex home: `{os.getenv("CODEX_HOME", "")}`
- Workspace: `{os.getenv("WORKSPACE") or os.getenv("CODEXON_WORKSPACE", "")}`
- Codex model: `{os.getenv("CODEX_MODEL", "")}`

## Service Contract

- Prefer editing or adding agents in `{AGENTS_DIR}`.
- Do not execute physical Home Assistant actions without explicit confirmation.
- Prefer deterministic logic over LLM calls for thresholds, timestamps, distances and deduplication.
- After changes, run tests with `python -m unittest discover -s tests -t . -v` when available.
- Use the web UI/API to create tasks and run agents manually.

## Current Status

```json
{json.dumps(status, ensure_ascii=False, indent=2)}
```

## Agents

{markdown_table(agents, ['name', 'enabled', 'effective_priority', 'effective_frequency_seconds', 'description'])}

## Recent Tasks

{markdown_table(tasks, ['id', 'status', 'run_at', 'priority', 'title', 'result', 'last_error'])}

## Recent Observations

{markdown_table(observations, ['created_at', 'source', 'summary'])}

## Operator Notes

{notes or '_Sin notas todavía._'}

## Recent Log Tail

```text
{log_tail or 'Sin logs.'}
```
"""


def write_codex_context() -> str:
    CODEX_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = build_codex_context()
    CODEX_CONTEXT_PATH.write_text(text, encoding="utf-8")
    return text


def backup_sources() -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    for path, arcname in [
        (DB_PATH, "data/codexon_memory.sqlite3"),
        (AGENT_CONFIG_PATH, "data/agent_config.json"),
        (CODEX_CONTEXT_PATH, "data/CODEX_CONTEXT.md"),
        (CODEX_NOTES_PATH, "data/CODEX_NOTES.md"),
        (LOG_PATH, "data/codexon_runtime.log"),
    ]:
        if path.exists():
            sources.append((path, arcname))
    if AGENTS_DIR.exists():
        sources.append((AGENTS_DIR, "agents"))
    return sources


def create_backup_archive(*, passphrase: str | None = None) -> dict[str, Any]:
    write_codex_context()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    tar_path = BACKUP_DIR / f"codexon-backup-{stamp}.tar.gz"
    manifest = {
        "created_at": utc_now(),
        "app": APP_NAME,
        "db_path": str(DB_PATH),
        "agents_dir": str(AGENTS_DIR),
        "included": [],
    }
    with tarfile.open(tar_path, "w:gz") as archive:
        for source, arcname in backup_sources():
            archive.add(source, arcname=arcname)
            manifest["included"].append(arcname)
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(dt.datetime.now(dt.UTC).timestamp())
        import io
        archive.addfile(info, io.BytesIO(manifest_bytes))
    key = passphrase if passphrase is not None else BACKUP_KEY
    if key:
        encrypted_path = tar_path.with_suffix(tar_path.suffix + ".enc")
        openssl = shutil.which("openssl")
        if not openssl:
            tar_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="openssl no esta disponible para cifrar el backup")
        subprocess.run(
            [openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-in", str(tar_path), "-out", str(encrypted_path), "-pass", "stdin"],
            input=key.encode("utf-8"),
            check=True,
        )
        tar_path.unlink(missing_ok=True)
        return {"path": str(encrypted_path), "encrypted": True, "bytes": encrypted_path.stat().st_size, "manifest": manifest}
    return {"path": str(tar_path), "encrypted": False, "bytes": tar_path.stat().st_size, "manifest": manifest}


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []
    rows = []
    for path in sorted(BACKUP_DIR.glob("codexon-backup-*"), reverse=True):
        if path.is_file():
            rows.append({"name": path.name, "path": str(path), "bytes": path.stat().st_size, "encrypted": path.name.endswith(".enc")})
    return rows


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    tasks = sqlite_rows("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")
    memories = sqlite_rows("SELECT COUNT(*) AS count FROM memories")
    observations = sqlite_rows("SELECT COUNT(*) AS count FROM observations")
    usage = usage_total()
    return {
        "app": APP_NAME,
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
        "log_path": str(LOG_PATH),
        "log_exists": LOG_PATH.exists(),
        "agent_config_path": str(AGENT_CONFIG_PATH),
        "tasks": tasks,
        "memories": memories[0]["count"] if memories else 0,
        "observations": observations[0]["count"] if observations else 0,
        "usage": usage,
        "tools": builtin_tool_names(include_homeassistant=bool(os.getenv("HA_TOKEN") or os.getenv("HOME_ASSISTANT_TOKEN"))),
        "whatsapp": whatsapp_status(include_qr=False),
    }


def whatsapp_status(*, include_qr: bool) -> dict[str, Any]:
    status_path = WHATSAPP_DATA_DIR / "status.json"
    status: dict[str, Any] = {
        "enabled": os.getenv("CODEXON_WHATSAPP_ENABLED", "").lower()
        in {"1", "true", "yes", "on"},
        "state": "disabled",
        "hasQr": False,
        "data_dir": str(WHATSAPP_DATA_DIR),
    }
    try:
        stored = json.loads(status_path.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            status.update(stored)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        status["error"] = str(exc)
    if include_qr:
        try:
            status["qrDataUrl"] = (
                WHATSAPP_DATA_DIR / "qr-data-url.txt"
            ).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            status["qrDataUrl"] = None
        except OSError as exc:
            status["qrDataUrl"] = None
            status["error"] = str(exc)
    return status


@app.get("/api/whatsapp")
def api_whatsapp() -> dict[str, Any]:
    return whatsapp_status(include_qr=True)


@app.get("/api/task-monitor")
def api_task_monitor(limit: int = 20) -> dict[str, Any]:
    conn = connect_db()
    try:
        return scheduler_monitor(conn, recent_limit=max(1, min(limit, 100)))
    finally:
        conn.close()


@app.get("/api/memories")
def api_memories(limit: int = 20) -> list[dict[str, Any]]:
    return sqlite_rows(
        """
        SELECT id, created_at, kind, topic, content, confidence, source
        FROM memories
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, min(limit, 100)),),
    )


@app.get("/api/observations")
def api_observations(limit: int = 30) -> list[dict[str, Any]]:
    return sqlite_rows(
        """
        SELECT id, created_at, source, summary, raw
        FROM observations
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, min(limit, 100)),),
    )


@app.get("/api/tasks")
def api_tasks(limit: int = 50, include_done: bool = True) -> list[dict[str, Any]]:
    where = "" if include_done else "WHERE status IN ('pending', 'running', 'failed')"
    return sqlite_rows(
        f"""
        SELECT id, created_at, updated_at, run_at, status, title, instruction,
               result, last_error, priority, interval_seconds, cancellation_key,
               attempts, max_attempts, retry_backoff_seconds, lease_owner,
               lease_expires_at, last_started_at, recovery_policy
        FROM tasks
        {where}
        ORDER BY run_at ASC, id ASC
        LIMIT ?
        """,
        (max(1, min(limit, 200)),),
    )


@app.get("/api/event-listeners")
def api_event_listeners(
    limit: int = 100, include_disabled: bool = True
) -> list[dict[str, Any]]:
    where = "" if include_disabled else "WHERE enabled = 1"
    return sqlite_rows(
        f"""
        SELECT id, created_at, updated_at, enabled, title, event_type, entity_id,
               from_state, to_state, attribute, operator, expected_value_json,
               event_data_json, instruction, cooldown_seconds, once_only,
               priority, cancellation_key, last_triggered_at, trigger_count
        FROM event_subscriptions
        {where}
        ORDER BY enabled DESC, created_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, min(limit, 500)),),
    )


@app.post("/api/event-listeners")
def api_create_event_listener(payload: dict[str, Any]) -> dict[str, Any]:
    conn = connect_db()
    try:
        try:
            listener_id = create_subscription(
                conn,
                title=str(payload.get("title") or ""),
                instruction=str(payload.get("instruction") or ""),
                event_type=str(payload.get("event_type") or "state_changed"),
                entity_id=str(payload.get("entity_id") or "") or None,
                from_state=(
                    str(payload["from_state"])
                    if payload.get("from_state") is not None
                    else None
                ),
                to_state=(
                    str(payload["to_state"])
                    if payload.get("to_state") is not None
                    else None
                ),
                attribute=str(payload.get("attribute") or "") or None,
                operator=str(payload.get("operator") or "") or None,
                expected_value=payload.get("expected_value"),
                event_data=payload.get("event_data"),
                cooldown_seconds=bounded_int(
                    payload.get("cooldown_seconds"),
                    field="cooldown_seconds",
                    minimum=0,
                    maximum=31_536_000,
                    default=0,
                ),
                once_only=bool(payload.get("once_only", False)),
                priority=bounded_int(
                    payload.get("priority"),
                    field="priority",
                    minimum=0,
                    maximum=100,
                    default=50,
                ),
                cancellation_key=normalize_cancellation_key(
                    str(payload.get("cancellation_key") or "")
                )
                or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()
    return {"ok": True, "id": listener_id}


@app.post("/api/event-listeners/{listener_id}/cancel")
def api_cancel_event_listener(listener_id: int) -> dict[str, Any]:
    conn = connect_db()
    try:
        changed = set_subscription_enabled(conn, listener_id, False)
    finally:
        conn.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Escucha no encontrada")
    return {"ok": True, "id": listener_id}


@app.post("/api/event-listeners/{listener_id}/enable")
def api_enable_event_listener(listener_id: int) -> dict[str, Any]:
    conn = connect_db()
    try:
        changed = set_subscription_enabled(conn, listener_id, True)
    finally:
        conn.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Escucha no encontrada")
    return {"ok": True, "id": listener_id}


@app.get("/api/event-listeners/{listener_id}/runs")
def api_event_listener_runs(listener_id: int, limit: int = 50) -> list[dict[str, Any]]:
    return sqlite_rows(
        """
        SELECT id, subscription_id, triggered_at, event_type, entity_id,
               task_id, event_json, error
        FROM event_trigger_runs
        WHERE subscription_id = ?
        ORDER BY triggered_at DESC, id DESC
        LIMIT ?
        """,
        (listener_id, max(1, min(limit, 200))),
    )


@app.post("/api/event-listeners/bulk-delete")
def api_bulk_delete_event_listeners(payload: dict[str, Any]) -> dict[str, Any]:
    ids = sorted(
        {
            int(value)
            for value in payload.get("ids", [])
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
    )
    if not ids:
        raise HTTPException(status_code=400, detail="Selecciona al menos una escucha")
    if len(ids) > 500:
        raise HTTPException(status_code=400, detail="Máximo 500 escuchas por operación")
    placeholders = ",".join("?" for _ in ids)
    conn = connect_db()
    try:
        existing = int(
            conn.execute(
                f"SELECT COUNT(*) FROM event_subscriptions WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchone()[0]
        )
        conn.execute(
            f"DELETE FROM event_trigger_runs WHERE subscription_id IN ({placeholders})",
            tuple(ids),
        )
        conn.execute(
            f"DELETE FROM event_subscriptions WHERE id IN ({placeholders})",
            tuple(ids),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": existing}


@app.post("/api/tasks")
def api_create_task(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    instruction = str(payload.get("instruction") or "").strip()
    if not title or not instruction:
        raise HTTPException(status_code=400, detail="title e instruction son obligatorios")
    run_at = parse_run_at(str(payload.get("run_at") or "") or None)
    priority = bounded_int(payload.get("priority"), field="priority", minimum=0, maximum=100, default=50)
    interval_seconds = parse_interval(payload.get("interval_seconds"))
    max_attempts = bounded_int(
        payload.get("max_attempts"), field="max_attempts", minimum=1, maximum=20, default=DEFAULT_TASK_MAX_ATTEMPTS
    )
    retry_backoff_seconds = bounded_int(
        payload.get("retry_backoff_seconds"),
        field="retry_backoff_seconds",
        minimum=1,
        maximum=86400,
        default=DEFAULT_TASK_RETRY_BACKOFF_SECONDS,
    )
    cancellation_key = normalize_cancellation_key(str(payload.get("cancellation_key") or "")) or None
    recovery_policy = task_recovery_policy(instruction)
    now = utc_now()
    task_id = execute_db(
        """
        INSERT INTO tasks(
            created_at, updated_at, run_at, status, title, instruction, priority,
            interval_seconds, cancellation_key, attempts, max_attempts,
            retry_backoff_seconds, recovery_policy
        )
        VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            now, now, run_at, title[:160], instruction, priority, interval_seconds,
            cancellation_key, max_attempts, retry_backoff_seconds, recovery_policy,
        ),
    )
    return {"ok": True, "id": task_id}


@app.patch("/api/tasks/{task_id}")
def api_update_task(task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "title": "title",
        "instruction": "instruction",
        "status": "status",
        "priority": "priority",
        "interval_seconds": "interval_seconds",
        "cancellation_key": "cancellation_key",
        "max_attempts": "max_attempts",
        "retry_backoff_seconds": "retry_backoff_seconds",
    }
    fields: list[str] = ["updated_at = ?"]
    values: list[Any] = [utc_now()]
    if "run_at" in payload:
        fields.append("run_at = ?")
        values.append(parse_run_at(str(payload.get("run_at") or "") or None))
    for key, column in allowed.items():
        if key in payload:
            value = payload[key]
            if key == "status":
                value = str(value or "").strip()
                if value not in VALID_TASK_STATUSES:
                    raise HTTPException(status_code=400, detail="status solo puede ser pending o cancelled")
            elif key == "priority":
                value = bounded_int(value, field=key, minimum=0, maximum=100, default=50)
            elif key == "interval_seconds":
                value = parse_interval(value)
            elif key == "max_attempts":
                value = bounded_int(value, field=key, minimum=1, maximum=20, default=DEFAULT_TASK_MAX_ATTEMPTS)
            elif key == "retry_backoff_seconds":
                value = bounded_int(value, field=key, minimum=1, maximum=86400, default=DEFAULT_TASK_RETRY_BACKOFF_SECONDS)
            elif key == "cancellation_key" and value in (None, ""):
                value = None
            elif key == "cancellation_key" and value is not None:
                value = normalize_cancellation_key(str(value)) or None
            elif value is not None:
                value = str(value)
                if key in {"title", "instruction"} and not value.strip():
                    raise HTTPException(status_code=400, detail=f"{key} no puede estar vacío")
            fields.append(f"{column} = ?")
            values.append(value)
            if key == "instruction" and value is not None:
                fields.append("recovery_policy = ?")
                values.append(task_recovery_policy(str(value)))
            if key == "status" and value in VALID_TASK_STATUSES:
                fields.extend(["lease_owner = NULL", "lease_expires_at = NULL"])
                if value == "pending":
                    fields.extend(["attempts = 0", "last_error = NULL"])
    values.append(task_id)
    changed = execute_db(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", tuple(values))
    if changed < 1:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return {"ok": True, "id": task_id}


@app.post("/api/tasks/{task_id}/run")
def api_run_task_now(task_id: int) -> dict[str, Any]:
    changed = execute_db(
        """
        UPDATE tasks SET updated_at = ?, run_at = ?, status = 'pending', last_error = NULL,
            attempts = 0, lease_owner = NULL, lease_expires_at = NULL
        WHERE id = ? AND status != 'running'
        """,
        (utc_now(), utc_now(), task_id),
    )
    if changed < 1:
        raise HTTPException(status_code=409, detail="La tarea está ejecutándose o no existe")
    return {"ok": True, "id": task_id}


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel_task(task_id: int) -> dict[str, Any]:
    conn = connect_db()
    now = utc_now()
    try:
        cursor = conn.execute(
            """
            UPDATE tasks SET updated_at = ?, status = 'cancelled', lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = ? AND status IN ('pending', 'running', 'failed')
            """,
            (now, task_id),
        )
        changed = cursor.rowcount
        if changed:
            conn.execute(
                """
                UPDATE task_runs SET finished_at = ?, status = 'cancelled',
                    error = 'Cancelada desde la API web'
                WHERE task_id = ? AND status = 'running'
                """,
                (now, task_id),
            )
        conn.commit()
    finally:
        conn.close()
    if changed < 1:
        raise HTTPException(status_code=409, detail="La tarea no está activa o no existe")
    return {"ok": True, "id": task_id}


@app.get("/api/tasks/{task_id}/runs")
def api_task_runs(task_id: int, limit: int = 50) -> list[dict[str, Any]]:
    return sqlite_rows(
        """
        SELECT id, task_id, worker_id, attempt, started_at, finished_at,
               status, result, error
        FROM task_runs
        WHERE task_id = ?
        ORDER BY started_at DESC, id DESC
        LIMIT ?
        """,
        (task_id, max(1, min(limit, 200))),
    )


@app.delete("/api/tasks")
def api_delete_all_tasks() -> dict[str, Any]:
    conn = connect_db()
    try:
        running = int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0])
        if running:
            raise HTTPException(status_code=409, detail="Hay tareas ejecutándose; cancélalas antes de eliminar todas")
        count = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        conn.execute("DELETE FROM task_runs")
        conn.execute("DELETE FROM tasks")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": count}


@app.post("/api/tasks/bulk-delete")
def api_bulk_delete_tasks(payload: dict[str, Any]) -> dict[str, Any]:
    ids = sorted(
        {
            int(value)
            for value in payload.get("ids", [])
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
    )
    if not ids:
        raise HTTPException(status_code=400, detail="Selecciona al menos una tarea")
    if len(ids) > 500:
        raise HTTPException(status_code=400, detail="Máximo 500 tareas por operación")
    placeholders = ",".join("?" for _ in ids)
    conn = connect_db()
    try:
        running = int(
            conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE id IN ({placeholders}) AND status = 'running'",
                tuple(ids),
            ).fetchone()[0]
        )
        if running:
            raise HTTPException(
                status_code=409,
                detail="La selección contiene tareas ejecutándose; cancélalas antes",
            )
        existing = int(
            conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchone()[0]
        )
        conn.execute(
            f"DELETE FROM task_runs WHERE task_id IN ({placeholders})", tuple(ids)
        )
        conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", tuple(ids))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": existing}


@app.delete("/api/tasks/{task_id}")
def api_delete_task(task_id: int) -> dict[str, Any]:
    conn = connect_db()
    try:
        cursor = conn.execute("DELETE FROM tasks WHERE id = ? AND status != 'running'", (task_id,))
        changed = cursor.rowcount
        if changed:
            conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    if changed < 1:
        raise HTTPException(status_code=409, detail="La tarea está ejecutándose o no existe")
    return {"ok": True, "id": task_id}


@app.get("/api/agents")
def api_agents() -> dict[str, Any]:
    manager = build_agent_manager()
    return {"agents": agent_rows(), "load_errors": manager.load_errors}


@app.patch("/api/agents/{name}")
def api_update_agent(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    known = {row["name"] for row in agent_rows()}
    if name not in known:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    data = read_agent_config()
    agents = data.setdefault("agents", {})
    current = agents.setdefault(name, {})
    for key in ("enabled", "priority", "frequency_seconds"):
        if key in payload:
            value = payload[key]
            if key == "enabled":
                current[key] = bool(value)
            else:
                current[key] = int(value)
    write_agent_config(data)
    return {"ok": True, "agent": name, "config": current}


@app.post("/api/agents/{name}/run")
async def api_run_agent(name: str) -> dict[str, Any]:
    manager = build_agent_manager()
    if name not in manager.agents:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    result = await manager.run_once(name)
    return {"ok": result.ok, "message": result.message, "data": result.data}






@app.get("/api/backups")
def api_backups() -> dict[str, Any]:
    return {"backup_dir": str(BACKUP_DIR), "backups": list_backups(), "key_configured": bool(BACKUP_KEY)}


@app.post("/api/backup")
def api_create_backup(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    passphrase = payload.get("passphrase")
    if passphrase is not None:
        passphrase = str(passphrase)
    return {"ok": True, "backup": create_backup_archive(passphrase=passphrase)}


@app.get("/api/codex/context")
def api_codex_context(refresh: bool = False) -> dict[str, Any]:
    text = write_codex_context() if refresh or not CODEX_CONTEXT_PATH.exists() else CODEX_CONTEXT_PATH.read_text(encoding="utf-8", errors="replace")
    return {"path": str(CODEX_CONTEXT_PATH), "text": text}


@app.post("/api/codex/context/refresh")
def api_refresh_codex_context() -> dict[str, Any]:
    text = write_codex_context()
    return {"ok": True, "path": str(CODEX_CONTEXT_PATH), "bytes": len(text.encode("utf-8"))}


@app.get("/api/codex/notes")
def api_codex_notes() -> dict[str, Any]:
    text = CODEX_NOTES_PATH.read_text(encoding="utf-8", errors="replace") if CODEX_NOTES_PATH.exists() else ""
    return {"path": str(CODEX_NOTES_PATH), "text": text}


@app.post("/api/codex/notes")
def api_update_codex_notes(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or "")
    CODEX_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_NOTES_PATH.write_text(text, encoding="utf-8")
    write_codex_context()
    return {"ok": True, "path": str(CODEX_NOTES_PATH)}


@app.get("/api/logs")
def api_logs(limit: int = 80) -> dict[str, Any]:
    return {"lines": tail_log(max(1, min(limit, 500)))}


@app.get("/api/tools")
def api_tools() -> dict[str, Any]:
    return {"tools": builtin_tool_names(include_homeassistant=bool(os.getenv("HA_TOKEN") or os.getenv("HOME_ASSISTANT_TOKEN")))}


@app.get("/api/models")
async def api_models(
    page: int = 1,
    page_size: int = MODEL_PAGE_SIZE,
    query: str = "",
    sort: str = "",
    configured_only: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    router = await build_model_router(refresh=refresh)
    selected_model = get_setting("interactive_model") or None
    models = router.configured_models() if configured_only else router.selectable_models()
    if query.strip():
        folded = normalize_cancellation_key(query)
        models = [
            model_id for model_id in models
            if folded in normalize_cancellation_key(f"{model_id} {(router.model_catalog.get(model_id) or {}).get('name') or ''}")
        ]
    models = [
        model_id for model_id in models
        if not router.model_catalog
        or (
            (router.model_catalog.get(model_id) or {}).get("supports_tools")
            and (router.model_catalog.get(model_id) or {}).get("supports_chat", True)
        )
    ]
    if sort in {"cost", "cost_asc", "precio", "precio_asc"}:
        def cost_sort_key(model_id: str) -> tuple[int, float, str]:
            meta = router.model_catalog.get(model_id) or {}
            input_price = float(meta.get("input_price_per_million") or 0)
            output_price = float(meta.get("output_price_per_million") or 0)
            if input_price < 0 or output_price < 0:
                return (1, 0, model_id)
            return (0, input_price + output_price, model_id)

        models.sort(
            key=cost_sort_key
        )
    page_size = max(10, min(int(page_size or MODEL_PAGE_SIZE), 100))
    total_pages = max(1, (len(models) + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), total_pages))
    start = (page - 1) * page_size
    visible = models[start : start + page_size]
    default_model = router.config.get("routes", {}).get("homeassistant", {}).get("model") or router.config.get("default")
    return {
        "selected_model": selected_model,
        "effective_model": selected_model or default_model,
        "automatic": selected_model is None,
        "default_model": default_model,
        "page": page,
        "page_size": page_size,
        "total": len(models),
        "total_pages": total_pages,
        "models": [model_row(router, model_id, selected_model) for model_id in visible],
    }


@app.post("/api/models/select")
async def api_select_model(payload: dict[str, Any]) -> dict[str, Any]:
    requested = str(payload.get("model") or "").strip()
    if requested.lower() in {"", "auto", "automatico", "automático", "router"}:
        set_setting("interactive_model", None)
        return {"ok": True, "selected_model": None}
    router = await build_model_router()
    selectable = set(router.selectable_models())
    if requested not in selectable:
        raise HTTPException(status_code=404, detail="Modelo no encontrado o no compatible con chat/tools")
    metadata = router.model_catalog.get(requested) or {}
    if metadata and not metadata.get("supports_tools"):
        raise HTTPException(status_code=400, detail="Ese modelo no declara soporte de herramientas")
    set_setting("interactive_model", requested)
    return {"ok": True, "selected_model": requested}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Codexon</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #17211f;
      --muted: #63716d;
      --line: #d7dfda;
      --panel: rgba(255,255,255,.9);
      --brand: #0f766e;
      --danger: #b91c1c;
      --warn: #b45309;
      --wash: #eef7f1;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: linear-gradient(135deg, #f7fbf8 0%, #edf5f2 52%, #f9f4e8 100%); min-height: 100vh; }
    main { width: 100%; max-width: 1600px; margin: 0 auto; padding: 24px 16px 44px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: end; margin-bottom: 18px; }
    h1 { font-size: clamp(2rem, 5vw, 4rem); line-height: .95; margin: 0; letter-spacing: 0; }
    h2 { font-size: 1rem; margin: 0 0 12px; }
    h3 { font-size: .95rem; margin: 14px 0 8px; }
    p { color: var(--muted); margin: 6px 0 0; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
    section, .stat { min-width: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; box-shadow: 0 12px 34px rgba(25, 45, 38, .08); }
    .stat { grid-column: span 3; min-height: 98px; }
    .stat strong { display: block; font-size: 1.8rem; line-height: 1; }
    .wide { grid-column: span 7; }
    .side { grid-column: span 5; }
    .full { grid-column: 1 / -1; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    label { display: grid; gap: 4px; color: var(--muted); font-size: .78rem; }
    input, textarea, select { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 8px; background: white; color: var(--ink); }
    textarea { min-height: 72px; resize: vertical; }
    button { border: 0; background: var(--ink); color: white; border-radius: 6px; padding: 8px 10px; cursor: pointer; }
    button.secondary { background: var(--brand); }
    button.warning { background: var(--warn); }
    button.danger { background: var(--danger); }
    button.ghost { color: var(--ink); background: white; border: 1px solid var(--line); }
    .tabs { display: flex; gap: 8px; overflow-x: auto; padding: 4px 0 14px; margin-bottom: 4px; }
    .tab { flex: 0 0 auto; color: var(--ink); background: rgba(255,255,255,.78); border: 1px solid var(--line); }
    .tab.active { color: white; background: var(--brand); border-color: var(--brand); }
    [data-page]:not(.active) { display: none !important; }
    .table-scroll { width: 100%; max-width: 100%; overflow-x: auto; overscroll-behavior-inline: contain; }
    table { width: 100%; min-width: 760px; border-collapse: collapse; font-size: .88rem; }
    th, td { text-align: left; border-top: 1px solid var(--line); padding: 8px; vertical-align: top; overflow-wrap: anywhere; }
    th { color: var(--muted); font-weight: 600; }
    .task-table { min-width: 1080px; table-layout: fixed; }
    .task-table .col-select, .listener-table .col-select { width: 42px; text-align: center; }
    .task-table .col-select input, .listener-table .col-select input { margin: 0; }
    .task-table .col-id { width: 58px; }
    .task-table .col-status { width: 90px; }
    .task-table .col-plan { width: 42%; }
    .task-table .col-result { width: auto; }
    .task-table .col-actions { width: 116px; }
    .task-table th:last-child, .task-table td:last-child { position: sticky; right: 0; background: #fff; box-shadow: -8px 0 12px rgba(23, 33, 31, .06); }
    .listener-table { min-width: 1120px; table-layout: fixed; }
    .listener-table .col-id { width: 58px; }
    .listener-table .col-status { width: 90px; }
    .listener-table .col-trigger { width: 28%; }
    .listener-table .col-action { width: 30%; }
    .listener-table .col-activity { width: auto; }
    .listener-table .col-actions { width: 122px; }
    .listener-table th:last-child, .listener-table td:last-child { position: sticky; right: 0; background: #fff; box-shadow: -8px 0 12px rgba(23, 33, 31, .06); }
    .model-list { display: grid; gap: 8px; }
    .model-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; border-top: 1px solid var(--line); padding: 10px 0; }
    .model-row:first-child { border-top: 0; }
    .model-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .price { font-variant-numeric: tabular-nums; }
    .pager { display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-top: 12px; }
    code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    pre { white-space: pre-wrap; margin: 0; max-height: 320px; overflow: auto; color: #263631; }
    .pill { display: inline-flex; border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; margin: 2px; background: rgba(255,255,255,.7); }
    .row-actions { display: grid; grid-template-columns: 1fr; gap: 5px; min-width: 96px; }
    .row-actions button { width: 100%; white-space: nowrap; }
    .muted { color: var(--muted); }
    .notice { position: sticky; top: 8px; z-index: 5; display: none; margin-bottom: 12px; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: white; }
    .notice.error { display: block; border-color: #fecaca; color: var(--danger); }
    .notice.ok { display: block; border-color: #99f6e4; color: var(--brand); }
    .split { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
    @media (max-width: 900px) { .stat, .wide, .side { grid-column: 1 / -1; } header { display: block; } .split { grid-template-columns: 1fr; } .model-row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <div id="notice" class="notice"></div>
    <header>
      <div>
        <h1>Codexon</h1>
        <p>Agentes, tareas, memoria y resultados desde la web.</p>
      </div>
      <div class="toolbar"><button onclick="loadAll()">Actualizar</button></div>
    </header>
    <nav class="tabs" aria-label="Zonas">
      <button class="tab active" data-tab="resumen" onclick="setPage('resumen')">Resumen</button>
      <button class="tab" data-tab="modelos" onclick="setPage('modelos')">Modelos</button>
      <button class="tab" data-tab="tareas" onclick="setPage('tareas')">Tareas</button>
      <button class="tab" data-tab="escuchas" onclick="setPage('escuchas')">Escuchas</button>
      <button class="tab" data-tab="monitor" onclick="setPage('monitor')">Monitor</button>
      <button class="tab" data-tab="agentes" onclick="setPage('agentes')">Agentes</button>
      <button class="tab" data-tab="sistema" onclick="setPage('sistema')">Sistema</button>
    </nav>

    <div class="grid">
      <div class="stat active" data-page="resumen"><p>Memorias</p><strong id="memories">-</strong></div>
      <div class="stat active" data-page="resumen"><p>Observaciones</p><strong id="observations">-</strong></div>
      <div class="stat active" data-page="resumen"><p>Llamadas LLM</p><strong id="calls">-</strong></div>
      <div class="stat active" data-page="resumen"><p>Coste estimado</p><strong id="cost">-</strong></div>

      <section class="full" data-page="modelos">
        <h2>Modelos IA</h2>
        <div class="toolbar">
          <label>Buscar<input id="modelQuery" placeholder="deepseek, gpt, gemini..." oninput="debouncedLoadModels()" /></label>
          <button class="ghost" onclick="selectModel('auto')">Automatico</button>
          <button class="secondary" onclick="loadModels(true)">Actualizar catalogo</button>
          <label><input id="modelSortCost" type="checkbox" onchange="modelState.page = 1; loadModels()" /> menor coste</label>
        </div>
        <p id="modelSummary" class="muted">-</p>
        <div id="modelList" class="model-list"></div>
        <div class="pager">
          <button class="ghost" onclick="changeModelPage(-1)">Anterior</button>
          <span id="modelPage" class="muted">-</span>
          <button class="ghost" onclick="changeModelPage(1)">Siguiente</button>
        </div>
      </section>

      <section class="full" data-page="agentes">
        <h2>Agentes</h2>
        <div class="table-scroll"><table><thead><tr><th>Agente</th><th>Estado</th><th>Intervalo</th><th>Prioridad</th><th>Ultimo resultado</th><th></th></tr></thead><tbody id="agents"></tbody></table></div>
      </section>

      <section class="full" data-page="tareas">
        <h2>Tareas</h2>
        <div class="split">
          <label>Titulo<input id="taskTitle" placeholder="Revision de sensores" /></label>
          <label>Fecha/hora<input id="taskRunAt" type="datetime-local" /></label>
          <label>Prioridad<input id="taskPriority" type="number" min="1" max="100" value="50" /></label>
          <label>Intervalo segundos<input id="taskInterval" type="number" min="0" placeholder="opcional" /></label>
        </div>
        <label>Instruccion<textarea id="taskInstruction" placeholder="Que debe hacer Codexon"></textarea></label>
        <div class="toolbar"><button class="secondary" onclick="createTask()">Crear tarea</button><button class="danger" onclick="deleteSelectedTasks()">Eliminar seleccionadas</button><button class="danger" onclick="deleteAllTasks()">Eliminar todas</button></div>
        <div class="table-scroll"><table class="task-table"><colgroup><col class="col-select"><col class="col-id"><col class="col-status"><col class="col-plan"><col class="col-result"><col class="col-actions"></colgroup><thead><tr><th class="col-select"><input id="selectAllTasks" type="checkbox" aria-label="Seleccionar todas las tareas" onchange="toggleGroupSelection('task-select', this.checked)" /></th><th>ID</th><th>Estado</th><th>Plan</th><th>Resultado</th><th>Acciones</th></tr></thead><tbody id="tasks"></tbody></table></div>
      </section>

      <section class="full" data-page="escuchas">
        <h2>Escuchas de eventos</h2>
        <p class="muted">Reglas persistentes que reaccionan a cambios de Home Assistant.</p>
        <div class="toolbar">
          <button class="secondary" onclick="loadAll()">Actualizar</button>
          <button class="danger" onclick="deleteSelectedListeners()">Eliminar seleccionadas</button>
        </div>
        <div class="table-scroll"><table class="listener-table"><colgroup><col class="col-select"><col class="col-id"><col class="col-status"><col class="col-trigger"><col class="col-action"><col class="col-activity"><col class="col-actions"></colgroup><thead><tr><th class="col-select"><input id="selectAllListeners" type="checkbox" aria-label="Seleccionar todas las escuchas" onchange="toggleGroupSelection('listener-select', this.checked)" /></th><th>ID</th><th>Estado</th><th>Disparador</th><th>Acción</th><th>Actividad</th><th>Acciones</th></tr></thead><tbody id="listeners"></tbody></table></div>
        <h3>Historial de la escucha</h3>
        <pre id="listenerRuns">Selecciona «Historial» en una escucha.</pre>
      </section>

      <section class="full" data-page="monitor">
        <h2>Monitor tareas</h2>
        <div class="split">
          <div class="stat"><p>Worker</p><strong id="monWorker">-</strong></div>
          <div class="stat"><p>Activas</p><strong id="monActive">-</strong></div>
          <div class="stat"><p>Atrasadas</p><strong id="monOverdue">-</strong></div>
          <div class="stat"><p>Exito</p><strong id="monSuccess">-</strong></div>
        </div>
        <div class="split">
          <div class="stat"><p>Llamadas LLM</p><strong id="monCalls">-</strong></div>
          <div class="stat"><p>Tokens</p><strong id="monTokens">-</strong></div>
          <div class="stat"><p>Coste</p><strong id="monCost">-</strong></div>
          <div class="stat"><p>Duracion media</p><strong id="monAvgRun">-</strong></div>
        </div>
        <section>
          <h2>Cola</h2>
          <pre id="monQueue"></pre>
        </section>
        <section>
          <h2>Uso por contexto</h2>
          <div class="table-scroll"><table><thead><tr><th>Contexto</th><th>Llamadas</th><th>Tokens</th><th>Coste</th><th>ms media</th></tr></thead><tbody id="monContext"></tbody></table></div>
        </section>
        <section>
          <h2>Uso por modelo</h2>
          <div class="table-scroll"><table><thead><tr><th>Modelo</th><th>Llamadas</th><th>Tokens</th><th>Coste</th><th>ms media</th></tr></thead><tbody id="monModels"></tbody></table></div>
        </section>
        <section>
          <h2>Ejecuciones recientes</h2>
          <div class="table-scroll"><table><thead><tr><th>ID</th><th>Tarea</th><th>Estado</th><th>Inicio</th><th>Fin</th><th>Resultado/Error</th></tr></thead><tbody id="monRuns"></tbody></table></div>
        </section>
      </section>

      <section class="side" data-page="sistema"><h2>Herramientas</h2><div id="tools"></div></section>
      <section class="side" data-page="sistema">
        <h2>Backups</h2>
        <p class="muted" id="backupDir">-</p>
        <label>Clave backup opcional<input id="backupPassphrase" type="password" placeholder="usa CODEXON_BACKUP_KEY si lo dejas vacio" /></label>
        <div class="toolbar"><button class="secondary" onclick="createBackup()">Crear backup</button></div>
        <ul id="backupList"></ul>
      </section>
      <section class="wide active" data-page="resumen"><h2>Observaciones recientes</h2><ul id="observationList"></ul></section>
      <section class="side active" data-page="resumen"><h2>Estado</h2><pre id="status"></pre></section>
      <section class="side active" data-page="resumen">
        <h2>WhatsApp</h2>
        <div id="whatsappStatus" class="muted">Cargando…</div>
        <img id="whatsappQr" alt="QR de vinculación de WhatsApp" style="display:none;width:260px;max-width:100%;margin-top:12px;image-rendering:pixelated" />
        <p id="whatsappHelp" class="muted"></p>
      </section>
      <section class="full" data-page="sistema">
        <h2>Codex mantenimiento</h2>
        <div class="toolbar"><button class="secondary" onclick="refreshCodexContext()">Actualizar contexto</button><span class="muted" id="codexPath">-</span></div>
        <label>Notas para enseñar/corregir Codexon<textarea id="codexNotes" placeholder="Criterios, decisiones, errores conocidos, ideas de nuevos agentes..."></textarea></label>
        <div class="toolbar"><button onclick="saveCodexNotes()">Guardar notas</button></div>
        <pre id="codexContext"></pre>
      </section>
      <section class="full" data-page="sistema"><h2>Logs</h2><pre id="logs"></pre></section>
    </div>
  </main>
<script>
async function requestJSON(url, options = {}) {
  const r = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || url);
  return data;
}
function text(v) { return v === null || v === undefined || v === '' ? '-' : String(v); }
function html(v) { return text(v).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
const modelState = {page: 1, totalPages: 1, loading: false};
function money(v) { return v === null || v === undefined ? 'variable' : '$' + Number(v || 0).toFixed(3) + '/M'; }
function compactNumber(v) { return v ? Number(v).toLocaleString('es-ES') : '-'; }
function setPage(name) {
  document.querySelectorAll('[data-page]').forEach(node => node.classList.toggle('active', node.dataset.page === name));
  document.querySelectorAll('.tab').forEach(node => node.classList.toggle('active', node.dataset.tab === name));
  if (name === 'modelos') loadModels();
}
function showNotice(message, kind = 'ok') {
  const node = document.getElementById('notice');
  node.textContent = message;
  node.className = 'notice ' + kind;
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => { node.className = 'notice'; }, 6000);
}
function isoFromLocal(value) { return value ? new Date(value).toISOString() : null; }
async function loadAll() {
  const [status, whatsapp, observations, tasks, listeners, monitor, logs, tools, agents, codex, notes, backups] = await Promise.all([
    requestJSON('/api/status'), requestJSON('/api/whatsapp'), requestJSON('/api/observations'), requestJSON('/api/tasks'), requestJSON('/api/event-listeners'), requestJSON('/api/task-monitor'), requestJSON('/api/logs'), requestJSON('/api/tools'), requestJSON('/api/agents'), requestJSON('/api/codex/context'), requestJSON('/api/codex/notes'), requestJSON('/api/backups')
  ]);
  document.getElementById('memories').textContent = status.memories;
  document.getElementById('observations').textContent = status.observations;
  document.getElementById('calls').textContent = status.usage.calls;
  document.getElementById('cost').textContent = '$' + Number(status.usage.cost || 0).toFixed(5);
  document.getElementById('status').textContent = JSON.stringify(status, null, 2);
  document.getElementById('whatsappStatus').textContent = whatsapp.enabled ? `Estado: ${text(whatsapp.state)} · mensajes: ${text(whatsapp.messageCount || 0)}` : 'Canal WhatsApp desactivado';
  const whatsappQr = document.getElementById('whatsappQr');
  whatsappQr.src = whatsapp.qrDataUrl || '';
  whatsappQr.style.display = whatsapp.qrDataUrl ? 'block' : 'none';
  document.getElementById('whatsappHelp').textContent = whatsapp.qrDataUrl ? 'Escanéalo desde WhatsApp > Dispositivos vinculados.' : (whatsapp.enabled && whatsapp.state !== 'connected' ? 'Revisa el estado y el log de Codexon.' : '');
  document.getElementById('logs').textContent = logs.lines.join('\\n') || 'Sin logs todavia.';
  document.getElementById('tools').innerHTML = tools.tools.map(t => `<span class="pill">${html(t)}</span>`).join('');
  document.getElementById('codexPath').textContent = codex.path;
  document.getElementById('codexContext').textContent = codex.text;
  document.getElementById('codexNotes').value = notes.text || '';
  document.getElementById('backupDir').textContent = backups.backup_dir + (backups.key_configured ? ' · clave configurada' : ' · sin clave configurada');
  document.getElementById('backupList').innerHTML = backups.backups.length ? backups.backups.map(b => `<li><strong>${html(b.name)}</strong><br><span class="muted">${html(b.bytes)} bytes · ${html(b.path)}</span></li>`).join('') : '<li>Sin backups.</li>';
  renderTasks(tasks);
  renderListeners(listeners);
  renderMonitor(monitor);
  renderAgents(agents.agents || []);
  document.getElementById('observationList').innerHTML = observations.length ? observations.map(o => `<li><strong>${html(o.source)}</strong> <span class="muted">${html(o.created_at)}</span><br>${html(o.summary)}</li>`).join('') : '<li>Sin observaciones todavia.</li>';
}
function renderMetricMoney(v) { return '$' + Number(v || 0).toFixed(5); }
function renderMs(v) { return Number(v || 0).toFixed(0) + ' ms'; }
function renderSeconds(v) { return Number(v || 0).toFixed(1) + ' s'; }
function renderMonitor(data) {
  const worker = data.worker || {};
  const queue = data.queue || {};
  const runs = data.runs || {};
  const usage = data.usage || {};
  const total = usage.total || {};
  document.getElementById('monWorker').textContent = worker.alive ? 'vivo' : 'sin pulso';
  document.getElementById('monActive').textContent = queue.active ?? 0;
  document.getElementById('monOverdue').textContent = queue.overdue ?? 0;
  document.getElementById('monSuccess').textContent = Number(runs.success_rate || 0).toFixed(1) + '%';
  document.getElementById('monCalls').textContent = total.calls ?? 0;
  document.getElementById('monTokens').textContent = compactNumber(total.total_tokens);
  document.getElementById('monCost').textContent = renderMetricMoney(total.cost_usd);
  document.getElementById('monAvgRun').textContent = renderSeconds(runs.avg_duration_seconds);
  document.getElementById('monQueue').textContent = JSON.stringify({worker, queue: {...queue, next_task: queue.next_task || '-'}, runs: {total: runs.total, failed: runs.failed, failure_rate: runs.failure_rate}}, null, 2);
  document.getElementById('monContext').innerHTML = (usage.by_context || []).map(row => `
    <tr><td>${html(row.context)}</td><td>${html(row.calls)}</td><td>${compactNumber(row.total_tokens)}</td><td>${renderMetricMoney(row.cost_usd)}</td><td>${renderMs(row.avg_ms)}</td></tr>
  `).join('') || '<tr><td colspan="5">Sin uso registrado.</td></tr>';
  document.getElementById('monModels').innerHTML = (usage.by_model || []).map(row => `
    <tr><td>${html(row.model)}</td><td>${html(row.calls)}</td><td>${compactNumber(row.total_tokens)}</td><td>${renderMetricMoney(row.cost_usd)}</td><td>${renderMs(row.avg_ms)}</td></tr>
  `).join('') || '<tr><td colspan="5">Sin uso registrado.</td></tr>';
  document.getElementById('monRuns').innerHTML = (runs.recent || []).map(row => `
    <tr><td>#${html(row.id)}</td><td>#${html(row.task_id)} ${html(row.title || '')}<br><span class="muted">${html(row.worker_id)}</span></td><td>${html(row.status)}<br><span class="muted">intento ${html(row.attempt)}</span></td><td>${html(row.started_at)}</td><td>${html(row.finished_at)}</td><td>${html(row.result || row.error || '-')}</td></tr>
  `).join('') || '<tr><td colspan="6">Sin ejecuciones.</td></tr>';
}
async function loadModels(refresh = false) {
  if (modelState.loading) return;
  modelState.loading = true;
  try {
    const query = document.getElementById('modelQuery').value || '';
    const sort = document.getElementById('modelSortCost').checked ? 'cost' : '';
    const data = await requestJSON(`/api/models?page=${modelState.page}&page_size=50&query=${encodeURIComponent(query)}&sort=${sort}&refresh=${refresh ? 'true' : 'false'}`);
    modelState.page = data.page;
    modelState.totalPages = data.total_pages;
    document.getElementById('modelSummary').textContent = `${data.automatic ? 'Automatico' : 'Seleccionado'}: ${data.effective_model || '-'} · ${data.total} modelos compatibles con chat/tools`;
    document.getElementById('modelPage').textContent = `Pagina ${data.page}/${data.total_pages}`;
    document.getElementById('modelList').innerHTML = data.models.length ? data.models.map(m => `
      <div class="model-row">
        <div>
          <strong>${html(m.id)}${m.selected ? ' *' : ''}</strong><br>
          <span class="muted">${html(m.name)}</span>
          <div class="model-meta">
            <span class="pill price">entrada ${money(m.input_price_per_million)}</span>
            <span class="pill price">salida ${money(m.output_price_per_million)}</span>
            <span class="pill">contexto ${compactNumber(m.context_length)}</span>
            ${m.configured ? '<span class="pill">configurado</span>' : ''}
            ${m.supports_structured_outputs ? '<span class="pill">JSON</span>' : ''}
          </div>
        </div>
        <button class="${m.selected ? 'ghost' : 'secondary'}" onclick="selectModel('${html(m.id)}')">${m.selected ? 'Activo' : 'Usar'}</button>
      </div>`).join('') : '<p class="muted">Sin modelos para ese filtro.</p>';
  } catch (err) {
    showNotice(err.message, 'error');
  } finally {
    modelState.loading = false;
  }
}
function debouncedLoadModels() {
  window.clearTimeout(debouncedLoadModels.timer);
  debouncedLoadModels.timer = window.setTimeout(() => { modelState.page = 1; loadModels(); }, 250);
}
async function changeModelPage(delta) {
  modelState.page = Math.max(1, Math.min(modelState.totalPages, modelState.page + delta));
  await loadModels();
}
async function selectModel(model) {
  try {
    const result = await requestJSON('/api/models/select', {method: 'POST', body: JSON.stringify({model})});
    showNotice(result.selected_model ? 'Modelo seleccionado: ' + result.selected_model : 'Modelo automatico activado');
    await loadModels();
  } catch (err) { showNotice(err.message, 'error'); }
}
function renderAgents(rows) {
  document.getElementById('agents').innerHTML = rows.length ? rows.map(a => `
    <tr>
      <td><strong>${html(a.name)}</strong><br><span class="muted">${html(a.description)}</span></td>
      <td><label><input type="checkbox" ${a.enabled ? 'checked' : ''} onchange="updateAgent('${html(a.name)}', {enabled: this.checked})" /> activo</label></td>
      <td><input type="number" value="${a.effective_frequency_seconds}" min="1" onchange="updateAgent('${html(a.name)}', {frequency_seconds: this.value})" /></td>
      <td><input type="number" value="${a.effective_priority}" min="1" max="100" onchange="updateAgent('${html(a.name)}', {priority: this.value})" /></td>
      <td class="muted">${html(a.stats?.last_message || '-')}</td>
      <td><button class="secondary" onclick="runAgent('${html(a.name)}')">Ejecutar</button></td>
    </tr>`).join('') : '<tr><td colspan="6">Sin agentes.</td></tr>';
}
function renderTasks(rows) {
  document.getElementById('selectAllTasks').checked = false;
  document.getElementById('tasks').innerHTML = rows.length ? rows.map(t => `
    <tr>
      <td class="col-select"><input class="task-select" type="checkbox" value="${t.id}" aria-label="Seleccionar tarea ${t.id}" /></td>
      <td>#${t.id}<br><span class="muted">prio ${html(t.priority)}</span></td>
      <td>${html(t.status)}</td>
      <td><strong>${html(t.title)}</strong><br><span class="mono">${html(t.run_at)}</span><br>${html(t.instruction)}<br><span class="muted">intervalo: ${html(t.interval_seconds)} · intentos: ${html(t.attempts)}/${html(t.max_attempts)} · clave: ${html(t.cancellation_key || '-')}</span></td>
      <td>${html(t.result || t.last_error || '-')}</td>
      <td><div class="row-actions"><button class="secondary" onclick="runTask(${t.id})">Ejecutar</button><button class="warning" onclick="cancelTask(${t.id})">Cancelar</button><button class="danger" onclick="deleteTask(${t.id})">Eliminar</button></div></td>
    </tr>`).join('') : '<tr><td colspan="6">Sin tareas.</td></tr>';
}
function listenerAction(listener) {
  const instruction = String(listener.instruction || '');
  const prefix = 'AUTOMATION_PLAN_V1 ';
  if (!instruction.startsWith(prefix)) return instruction || '-';
  try {
    const plan = JSON.parse(instruction.slice(prefix.length));
    return (plan.steps || []).map(step => {
      if (step.type === 'service') {
        const target = step.target?.entity_id;
        const targets = Array.isArray(target) ? target.join(', ') : target;
        return `${step.domain}.${step.service} → ${targets || '-'}`;
      }
      if (step.type === 'delay') return `espera ${step.seconds}s`;
      return `${step.type || 'acción'} ${step.name || ''}`.trim();
    }).join(' · ') || plan.name || '-';
  } catch (_) {
    return instruction;
  }
}
function listenerTrigger(listener) {
  const transition = listener.from_state !== null || listener.to_state !== null
    ? `${text(listener.from_state)} → ${text(listener.to_state)}`
    : 'cualquier cambio';
  let condition = '';
  if (listener.operator) {
    let expected = listener.expected_value_json;
    try { expected = JSON.parse(expected); } catch (_) {}
    condition = ` · ${listener.attribute || 'estado'} ${listener.operator} ${text(expected)}`;
  }
  return `${listener.event_type} · ${listener.entity_id || 'todos'} · ${transition}${condition}`;
}
function renderListeners(rows) {
  document.getElementById('selectAllListeners').checked = false;
  document.getElementById('listeners').innerHTML = rows.length ? rows.map(listener => `
    <tr>
      <td class="col-select"><input class="listener-select" type="checkbox" value="${listener.id}" aria-label="Seleccionar escucha ${listener.id}" /></td>
      <td>#${html(listener.id)}<br><span class="muted">prio ${html(listener.priority)}</span></td>
      <td><strong>${listener.enabled ? 'Activa' : 'Inactiva'}</strong><br><span class="muted">${listener.once_only ? 'una vez' : 'persistente'}</span></td>
      <td><strong>${html(listener.title)}</strong><br><span class="mono">${html(listenerTrigger(listener))}</span><br><span class="muted">cooldown: ${html(listener.cooldown_seconds)} s</span></td>
      <td>${html(listenerAction(listener))}<br><span class="muted">clave: ${html(listener.cancellation_key || '-')}</span></td>
      <td>${html(listener.trigger_count)} disparos<br><span class="muted">último: ${html(listener.last_triggered_at || '-')}</span></td>
      <td><div class="row-actions">${listener.enabled
        ? `<button class="warning" onclick="cancelListener(${listener.id})">Cancelar</button>`
        : `<button class="secondary" onclick="enableListener(${listener.id})">Reactivar</button>`}
        <button class="ghost" onclick="loadListenerRuns(${listener.id})">Historial</button></div></td>
    </tr>`).join('') : '<tr><td colspan="7">Sin escuchas.</td></tr>';
}
function toggleGroupSelection(className, checked) {
  document.querySelectorAll('.' + className).forEach(item => { item.checked = checked; });
}
function selectedIds(className) {
  return Array.from(document.querySelectorAll('.' + className + ':checked')).map(item => Number(item.value));
}
async function createTask() {
  try {
    await requestJSON('/api/tasks', {method: 'POST', body: JSON.stringify({
      title: document.getElementById('taskTitle').value,
      instruction: document.getElementById('taskInstruction').value,
      run_at: isoFromLocal(document.getElementById('taskRunAt').value),
      priority: document.getElementById('taskPriority').value,
      interval_seconds: document.getElementById('taskInterval').value || null,
      cancellation_key: null
    })});
    document.getElementById('taskTitle').value = '';
    document.getElementById('taskInstruction').value = '';
    showNotice('Tarea creada');
    await loadAll();
  } catch (err) { showNotice(err.message, 'error'); }
}
async function runTask(id) { try { await requestJSON(`/api/tasks/${id}/run`, {method: 'POST'}); showNotice('Tarea preparada para ejecutar'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function cancelTask(id) { try { await requestJSON(`/api/tasks/${id}/cancel`, {method: 'POST'}); showNotice('Tarea cancelada'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function cancelListener(id) {
  if (!confirm('¿Cancelar la escucha #' + id + '?')) return;
  try {
    await requestJSON(`/api/event-listeners/${id}/cancel`, {method: 'POST'});
    showNotice('Escucha cancelada');
    await loadAll();
  } catch (err) { showNotice(err.message, 'error'); }
}
async function enableListener(id) {
  try {
    await requestJSON(`/api/event-listeners/${id}/enable`, {method: 'POST'});
    showNotice('Escucha reactivada');
    await loadAll();
  } catch (err) { showNotice(err.message, 'error'); }
}
async function loadListenerRuns(id) {
  try {
    const rows = await requestJSON(`/api/event-listeners/${id}/runs`);
    document.getElementById('listenerRuns').textContent = rows.length
      ? JSON.stringify(rows.map(row => ({
          id: row.id,
          triggered_at: row.triggered_at,
          event_type: row.event_type,
          entity_id: row.entity_id,
          task_id: row.task_id,
          error: row.error
        })), null, 2)
      : `La escucha #${id} no tiene disparos registrados.`;
  } catch (err) { showNotice(err.message, 'error'); }
}
async function deleteTask(id) { if (confirm('Eliminar tarea #' + id + '?')) { try { await requestJSON(`/api/tasks/${id}`, {method: 'DELETE'}); showNotice('Tarea eliminada'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } } }
async function deleteSelectedTasks() {
  const ids = selectedIds('task-select');
  if (!ids.length) { showNotice('Selecciona al menos una tarea', 'error'); return; }
  if (!confirm(`¿Eliminar definitivamente ${ids.length} tareas seleccionadas y su historial?`)) return;
  try {
    const result = await requestJSON('/api/tasks/bulk-delete', {method: 'POST', body: JSON.stringify({ids})});
    showNotice(`Tareas eliminadas: ${result.deleted}`);
    await loadAll();
  } catch (err) { showNotice(err.message, 'error'); }
}
async function deleteSelectedListeners() {
  const ids = selectedIds('listener-select');
  if (!ids.length) { showNotice('Selecciona al menos una escucha', 'error'); return; }
  if (!confirm(`¿Eliminar definitivamente ${ids.length} escuchas seleccionadas y su historial?`)) return;
  try {
    const result = await requestJSON('/api/event-listeners/bulk-delete', {method: 'POST', body: JSON.stringify({ids})});
    showNotice(`Escuchas eliminadas: ${result.deleted}`);
    document.getElementById('listenerRuns').textContent = 'Selecciona «Historial» en una escucha.';
    await loadAll();
  } catch (err) { showNotice(err.message, 'error'); }
}
async function deleteAllTasks() { if (confirm('¿Eliminar definitivamente todas las tareas y su historial de ejecuciones?')) { try { const result = await requestJSON('/api/tasks', {method: 'DELETE'}); showNotice(`Tareas eliminadas: ${result.deleted}`); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } } }
async function updateAgent(name, payload) { try { await requestJSON(`/api/agents/${encodeURIComponent(name)}`, {method: 'PATCH', body: JSON.stringify(payload)}); showNotice('Agente actualizado'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function runAgent(name) { try { const result = await requestJSON(`/api/agents/${encodeURIComponent(name)}/run`, {method: 'POST'}); showNotice(result.message, result.ok ? 'ok' : 'error'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function refreshCodexContext() { try { await requestJSON('/api/codex/context/refresh', {method: 'POST'}); showNotice('Contexto Codex actualizado'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function createBackup() { try { const pass = document.getElementById('backupPassphrase').value; const result = await requestJSON('/api/backup', {method: 'POST', body: JSON.stringify({passphrase: pass || null})}); showNotice('Backup creado: ' + result.backup.path); document.getElementById('backupPassphrase').value = ''; await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
async function saveCodexNotes() { try { await requestJSON('/api/codex/notes', {method: 'POST', body: JSON.stringify({text: document.getElementById('codexNotes').value})}); showNotice('Notas Codex guardadas'); await loadAll(); } catch (err) { showNotice(err.message, 'error'); } }
loadAll().catch(err => { showNotice(err.message, 'error'); });
</script>
</body>
</html>
"""
