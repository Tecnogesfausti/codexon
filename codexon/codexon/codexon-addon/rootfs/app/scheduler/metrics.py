from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any


ACTIVE_STATUSES = ("pending", "running", "failed")
DONE_STATUSES = ("done", "cancelled")


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _seconds_between(start: str | None, finish: str | None) -> float | None:
    started = _parse_time(start)
    finished = _parse_time(finish)
    if started is None or finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


def _percent(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 1)


def scheduler_monitor(conn: sqlite3.Connection, *, recent_limit: int = 20) -> dict[str, Any]:
    now = _utc_now()
    status_rows = list(
        conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM tasks
            GROUP BY status
            ORDER BY status
            """
        )
    )
    by_status = {str(row["status"]): int(row["count"]) for row in status_rows}

    next_task = _row_dict(
        conn.execute(
            """
            SELECT id, run_at, title, priority, interval_seconds, attempts, max_attempts
            FROM tasks
            WHERE status = 'pending'
            ORDER BY priority DESC, run_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
    )
    overdue = int(
        conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'pending' AND run_at <= ?",
            (now.isoformat(timespec="seconds"),),
        ).fetchone()[0]
    )
    running_expired = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM tasks
            WHERE status = 'running'
              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            """,
            (now.isoformat(timespec="seconds"),),
        ).fetchone()[0]
    )

    run_summary = _row_dict(
        conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN status IN ('failed', 'timeout') THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END) AS interrupted,
                SUM(CASE WHEN status = 'rescheduled' THEN 1 ELSE 0 END) AS rescheduled
            FROM task_runs
            """
        ).fetchone()
    )
    total_runs = int(run_summary.get("total") or 0)
    done_runs = int(run_summary.get("done") or 0)
    failed_runs = int(run_summary.get("failed") or 0)

    finished_runs = list(
        conn.execute(
            """
            SELECT started_at, finished_at
            FROM task_runs
            WHERE finished_at IS NOT NULL
            ORDER BY finished_at DESC, id DESC
            LIMIT 100
            """
        )
    )
    durations = [
        seconds
        for seconds in (
            _seconds_between(str(row["started_at"] or ""), str(row["finished_at"] or ""))
            for row in finished_runs
        )
        if seconds is not None
    ]
    avg_duration = round(sum(durations) / len(durations), 2) if durations else 0.0
    max_duration = round(max(durations), 2) if durations else 0.0

    usage_total = _row_dict(
        conn.execute(
            """
            SELECT
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN estimated_cost_usd > 0 THEN estimated_cost_usd ELSE 0 END), 0) AS cost_usd,
                COALESCE(AVG(duration_ms), 0) AS avg_llm_ms
            FROM usage_events
            """
        ).fetchone()
    )
    usage_by_context = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                COALESCE(context, '-') AS context,
                COUNT(*) AS calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN estimated_cost_usd > 0 THEN estimated_cost_usd ELSE 0 END), 0) AS cost_usd,
                COALESCE(AVG(duration_ms), 0) AS avg_ms
            FROM usage_events
            GROUP BY COALESCE(context, '-')
            ORDER BY cost_usd DESC, total_tokens DESC, calls DESC
            LIMIT 12
            """
        )
    ]
    usage_by_model = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                model,
                COUNT(*) AS calls,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN estimated_cost_usd > 0 THEN estimated_cost_usd ELSE 0 END), 0) AS cost_usd,
                COALESCE(AVG(duration_ms), 0) AS avg_ms
            FROM usage_events
            GROUP BY model
            ORDER BY cost_usd DESC, total_tokens DESC, calls DESC
            LIMIT 12
            """
        )
    ]
    recent_runs = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                r.id, r.task_id, r.worker_id, r.attempt, r.started_at, r.finished_at,
                r.status, r.result, r.error, t.title
            FROM task_runs r
            LEFT JOIN tasks t ON t.id = r.task_id
            ORDER BY r.started_at DESC, r.id DESC
            LIMIT ?
            """,
            (max(1, min(int(recent_limit), 100)),),
        )
    ]

    settings = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            """
            SELECT key, value
            FROM settings
            WHERE key LIKE 'scheduler.%'
            """
        )
    }
    heartbeat_at = _parse_time(settings.get("scheduler.heartbeat_at"))
    heartbeat_age_seconds = (
        round((now - heartbeat_at).total_seconds(), 1) if heartbeat_at is not None else None
    )
    worker_alive = heartbeat_age_seconds is not None and heartbeat_age_seconds <= 90

    if next_task:
        parsed_next = _parse_time(str(next_task.get("run_at") or ""))
        next_task["seconds_until_run"] = (
            round((parsed_next - now).total_seconds(), 1) if parsed_next is not None else None
        )

    return {
        "worker": {
            "alive": worker_alive,
            "worker_id": settings.get("scheduler.worker_id"),
            "started_at": settings.get("scheduler.started_at"),
            "heartbeat_at": settings.get("scheduler.heartbeat_at"),
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "last_recovered": int(settings.get("scheduler.last_recovered") or 0),
            "last_due_count": int(settings.get("scheduler.last_due_count") or 0),
            "last_wait_seconds": float(settings.get("scheduler.last_wait_seconds") or 0),
        },
        "queue": {
            "by_status": by_status,
            "active": sum(by_status.get(status, 0) for status in ACTIVE_STATUSES),
            "completed": sum(by_status.get(status, 0) for status in DONE_STATUSES),
            "overdue": overdue,
            "running_expired": running_expired,
            "next_task": next_task or None,
        },
        "runs": {
            "total": total_runs,
            "done": done_runs,
            "failed": failed_runs,
            "interrupted": int(run_summary.get("interrupted") or 0),
            "rescheduled": int(run_summary.get("rescheduled") or 0),
            "success_rate": _percent(done_runs, total_runs),
            "failure_rate": _percent(failed_runs, total_runs),
            "avg_duration_seconds": avg_duration,
            "max_duration_seconds": max_duration,
            "recent": recent_runs,
        },
        "usage": {
            "total": usage_total,
            "by_context": usage_by_context,
            "by_model": usage_by_model,
        },
    }


def format_scheduler_monitor(data: dict[str, Any]) -> str:
    worker = data.get("worker") or {}
    queue = data.get("queue") or {}
    runs = data.get("runs") or {}
    usage = data.get("usage") or {}
    total_usage = usage.get("total") or {}
    next_task = queue.get("next_task") or {}
    by_status = queue.get("by_status") or {}
    worker_status = "vivo" if worker.get("alive") else "sin pulso"
    next_text = "ninguna"
    if next_task:
        seconds = next_task.get("seconds_until_run")
        delay_text = f" en {float(seconds):.1f}s" if isinstance(seconds, (int, float)) else ""
        next_text = f"#{next_task.get('id')} {next_task.get('title')} ({next_task.get('run_at')}){delay_text}"
    lines = [
        "Codexon ntop tareas",
        f"Worker: {worker_status} · {worker.get('worker_id') or '-'} · heartbeat {worker.get('heartbeat_age_seconds') if worker.get('heartbeat_age_seconds') is not None else '-'}s",
        f"Cola: activas {queue.get('active', 0)} · atrasadas {queue.get('overdue', 0)} · leases expirados {queue.get('running_expired', 0)} · estados {by_status}",
        f"Proxima: {next_text}",
        (
            "Ejecuciones: "
            f"{runs.get('total', 0)} total · {runs.get('done', 0)} ok · {runs.get('failed', 0)} fallos · "
            f"exito {float(runs.get('success_rate') or 0):.1f}% · media {float(runs.get('avg_duration_seconds') or 0):.1f}s"
        ),
        (
            "LLM: "
            f"{total_usage.get('calls', 0)} llamadas · {int(total_usage.get('total_tokens') or 0):,} tokens · "
            f"${float(total_usage.get('cost_usd') or 0):.5f} estimado"
        ).replace(",", "."),
    ]
    recent = runs.get("recent") or []
    if recent:
        lines.append("Ultimas ejecuciones:")
        for row in recent[:5]:
            detail = row.get("result") or row.get("error") or "-"
            detail = str(detail).replace("\n", " ")
            if len(detail) > 140:
                detail = detail[:137] + "..."
            lines.append(
                f"- run #{row.get('id')} tarea #{row.get('task_id')} {row.get('status')} "
                f"{row.get('started_at')} · {row.get('title') or '-'} · {detail}"
            )
    return "\n".join(lines)
