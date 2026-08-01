from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from typing import Any


RuntimeLogger = Callable[..., None]
ExceptionFormatter = Callable[[BaseException], str]


async def run_task_loop(
    agent: Any,
    stop: asyncio.Event,
    *,
    task_timeout_seconds: int,
    runtime_log: RuntimeLogger,
    exception_summary: ExceptionFormatter,
    utc_now: Callable[[], str],
    status_writer: Callable[[str], None] | None = None,
) -> None:
    def write_status(message: str) -> None:
        if status_writer is not None:
            status_writer(message)
        else:
            print(message, flush=True)

    recovered = agent.memory.recover_expired_tasks()
    agent.memory.set_setting("scheduler.worker_id", agent.worker_id)
    agent.memory.set_setting("scheduler.started_at", utc_now())
    agent.memory.set_setting("scheduler.last_recovered", str(recovered))
    runtime_log("info", "task_loop", "started", worker_id=agent.worker_id, recovered=recovered)
    while not stop.is_set():
        agent.memory.set_setting("scheduler.heartbeat_at", utc_now())
        recovered = agent.memory.recover_expired_tasks()
        agent.memory.set_setting("scheduler.last_recovered", str(recovered))
        if recovered:
            runtime_log("warn", "task_loop", "expired_tasks_recovered", count=recovered)
        due_tasks = agent.memory.get_due_tasks(limit=20)
        agent.memory.set_setting("scheduler.last_due_count", str(len(due_tasks)))
        for task in due_tasks:
            task_id = int(task["id"])
            run_id = agent.memory.mark_task_running(task_id, agent.worker_id)
            if run_id is None:
                continue
            try:
                write_status(f"\n[tarea #{task_id}] ejecutando: {task['title']}")
                runtime_log(
                    "info",
                    "task_loop",
                    "task_started",
                    task_id=task_id,
                    run_id=run_id,
                    worker_id=agent.worker_id,
                    attempt=int(task["attempts"] or 0) + 1,
                    title=str(task["title"]),
                )
                result = await asyncio.wait_for(
                    agent.execute_scheduled_instruction(
                        task_id=task_id,
                        run_id=run_id,
                        title=str(task["title"]),
                        instruction=str(task["instruction"]),
                    ),
                    timeout=task_timeout_seconds,
                )
                if task_id in agent.rescheduled_task_ids:
                    agent.rescheduled_task_ids.discard(task_id)
                    runtime_log("info", "task_loop", "task_rescheduled", task_id=task_id, run_id=run_id, result=result[:500])
                    write_status(f"\n[tarea #{task_id}] reprogramada")
                else:
                    interval_seconds = int(task["interval_seconds"] or 0) if "interval_seconds" in task.keys() else 0
                    if re.search(r"\breprogram", result, flags=re.IGNORECASE) and interval_seconds <= 0:
                        message = "La tarea dijo que se reprogramaba, pero no llamo reschedule_current_task."
                        outcome = agent.memory.fail_or_retry_task(task, agent.worker_id, run_id, message)
                        runtime_log("error", "task_loop", "task_failed", task_id=task_id, run_id=run_id, error=message, outcome=outcome, result=result[:500])
                        write_status(f"\n[tarea #{task_id}] error: {message}")
                    elif interval_seconds > 0:
                        result = await agent.send_scheduled_completion_alert(
                            title=str(task["title"]),
                            instruction=str(task["instruction"]),
                            result=result,
                        )
                        next_run = agent.memory.reschedule_after_success(task, agent.worker_id, run_id, result)
                        if next_run:
                            runtime_log("info", "task_loop", "task_recurring_rescheduled", task_id=task_id, run_id=run_id, next_run=next_run, interval_seconds=interval_seconds, result=result[:500])
                            write_status(f"\n[tarea #{task_id}] recurrente reprogramada para {next_run}")
                        else:
                            runtime_log("info", "task_loop", "task_not_rescheduled_status_changed", task_id=task_id, run_id=run_id, result=result[:500])
                            write_status(f"\n[tarea #{task_id}] no reprogramada: estado cambiado durante la ejecución")
                    else:
                        result = await agent.send_scheduled_completion_alert(
                            title=str(task["title"]),
                            instruction=str(task["instruction"]),
                            result=result,
                        )
                        completed = agent.memory.mark_task_done(task_id, agent.worker_id, run_id, result)
                        if completed:
                            runtime_log("info", "task_loop", "task_done", task_id=task_id, run_id=run_id, result=result[:500])
                            write_status(f"\n[tarea #{task_id}] completada")
                        else:
                            runtime_log("info", "task_loop", "task_completion_ignored_status_changed", task_id=task_id, run_id=run_id)
            except asyncio.TimeoutError:
                message = f"Tiempo agotado tras {task_timeout_seconds}s"
                outcome = agent.memory.fail_or_retry_task(
                    task,
                    agent.worker_id,
                    run_id,
                    message,
                    timed_out=True,
                )
                runtime_log("warn", "task_loop", "task_timeout", task_id=task_id, run_id=run_id, timeout=task_timeout_seconds, outcome=outcome)
                write_status(f"\n[tarea #{task_id}] {message}; estado {outcome['status']}")
            except Exception as exc:  # noqa: BLE001
                message = exception_summary(exc)
                outcome = agent.memory.fail_or_retry_task(task, agent.worker_id, run_id, message)
                runtime_log("error", "task_loop", "task_failed", task_id=task_id, run_id=run_id, error=message, outcome=outcome)
                write_status(f"\n[tarea #{task_id}] error: {exc}; estado {outcome['status']}")
            finally:
                agent.current_task_id = None
                agent.current_task_run_id = None
                agent.rescheduled_task_ids.discard(task_id)

        wait_seconds = agent.memory.seconds_until_next_task()
        agent.memory.set_setting("scheduler.last_wait_seconds", f"{wait_seconds:.3f}")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=wait_seconds)
