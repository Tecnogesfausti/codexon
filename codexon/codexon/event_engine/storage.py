from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any


VALID_OPERATORS = {"eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in"}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def ensure_event_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            title TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'state_changed',
            entity_id TEXT,
            from_state TEXT,
            to_state TEXT,
            attribute TEXT,
            operator TEXT,
            expected_value_json TEXT,
            event_data_json TEXT,
            instruction TEXT NOT NULL,
            cooldown_seconds INTEGER NOT NULL DEFAULT 0,
            once_only INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 50,
            cancellation_key TEXT,
            last_triggered_at TEXT,
            trigger_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS event_trigger_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            triggered_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_id TEXT,
            task_id INTEGER,
            event_json TEXT,
            error TEXT,
            FOREIGN KEY(subscription_id) REFERENCES event_subscriptions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_event_subscriptions_enabled
            ON event_subscriptions(enabled, event_type, entity_id);
        CREATE INDEX IF NOT EXISTS idx_event_trigger_runs_subscription
            ON event_trigger_runs(subscription_id, triggered_at DESC);
        """
    )
    conn.commit()


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def create_subscription(
    conn: sqlite3.Connection,
    *,
    title: str,
    instruction: str,
    event_type: str = "state_changed",
    entity_id: str | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    attribute: str | None = None,
    operator: str | None = None,
    expected_value: Any = None,
    event_data: dict[str, Any] | None = None,
    cooldown_seconds: int = 0,
    once_only: bool = False,
    priority: int = 50,
    cancellation_key: str | None = None,
) -> int:
    title = title.strip()
    instruction = instruction.strip()
    event_type = event_type.strip()
    entity_id = entity_id.strip() if entity_id else None
    if not title or not instruction or not event_type:
        raise ValueError("title, instruction y event_type son obligatorios")
    if event_type == "state_changed" and not entity_id:
        raise ValueError("entity_id es obligatorio para state_changed")
    if operator is not None and operator not in VALID_OPERATORS:
        raise ValueError(f"Operador no soportado: {operator}")
    if operator is not None and expected_value is None:
        raise ValueError("expected_value es obligatorio cuando se usa operator")
    cooldown_seconds = max(0, min(int(cooldown_seconds), 31_536_000))
    priority = max(0, min(int(priority), 100))
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO event_subscriptions(
            created_at, updated_at, enabled, title, event_type, entity_id,
            from_state, to_state, attribute, operator, expected_value_json,
            event_data_json, instruction, cooldown_seconds, once_only, priority,
            cancellation_key
        )
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            now,
            title[:160],
            event_type[:120],
            entity_id,
            from_state,
            to_state,
            attribute,
            operator,
            _json_dump(expected_value),
            _json_dump(event_data),
            instruction,
            cooldown_seconds,
            int(bool(once_only)),
            priority,
            cancellation_key,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def list_subscriptions(
    conn: sqlite3.Connection, *, enabled_only: bool = False, limit: int = 200
) -> list[sqlite3.Row]:
    where = "WHERE enabled = 1" if enabled_only else ""
    return list(
        conn.execute(
            f"""
            SELECT *
            FROM event_subscriptions
            {where}
            ORDER BY enabled DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 1000)),),
        )
    )


def set_subscription_enabled(conn: sqlite3.Connection, subscription_id: int, enabled: bool) -> bool:
    cursor = conn.execute(
        "UPDATE event_subscriptions SET enabled = ?, updated_at = ? WHERE id = ?",
        (int(enabled), utc_now(), int(subscription_id)),
    )
    conn.commit()
    return cursor.rowcount == 1


def claim_trigger(
    conn: sqlite3.Connection, subscription_id: int, *, now: str | None = None
) -> int | None:
    now = now or utc_now()
    row = conn.execute(
        """
        SELECT enabled, once_only, cooldown_seconds, last_triggered_at, trigger_count
        FROM event_subscriptions WHERE id = ?
        """,
        (int(subscription_id),),
    ).fetchone()
    if row is None or not int(row["enabled"]):
        return None
    last_triggered_at = row["last_triggered_at"]
    if last_triggered_at:
        previous = dt.datetime.fromisoformat(str(last_triggered_at).replace("Z", "+00:00"))
        current = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        if (current - previous).total_seconds() < int(row["cooldown_seconds"] or 0):
            return None
    trigger_count = int(row["trigger_count"] or 0) + 1
    conn.execute(
        """
        UPDATE event_subscriptions
        SET updated_at = ?, last_triggered_at = ?, trigger_count = ?,
            enabled = CASE WHEN once_only = 1 THEN 0 ELSE enabled END
        WHERE id = ? AND enabled = 1
        """,
        (now, now, trigger_count, int(subscription_id)),
    )
    conn.commit()
    return trigger_count


def add_trigger_run(
    conn: sqlite3.Connection,
    *,
    subscription_id: int,
    event_type: str,
    entity_id: str | None,
    event: dict[str, Any],
    task_id: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO event_trigger_runs(
            subscription_id, triggered_at, event_type, entity_id, task_id,
            event_json, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(subscription_id),
            utc_now(),
            event_type,
            entity_id,
            task_id,
            json.dumps(event, ensure_ascii=False, separators=(",", ":"))[:100_000],
            error,
        ),
    )
    conn.commit()
