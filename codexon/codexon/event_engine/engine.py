from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from typing import Any, Callable

from automation.engine import compare_values
from event_engine.storage import add_trigger_run, claim_trigger, list_subscriptions
from tools.homeassistant import ha_websocket_url


RuntimeLogger = Callable[..., None]


def _decoded(row: Any, key: str) -> Any:
    raw = row[key]
    return json.loads(raw) if raw not in (None, "") else None


def _nested_value(value: Any, path: str | None) -> Any:
    if not path:
        return value
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _event_data_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _event_data_matches(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return actual == expected
    return actual == expected


def event_matches_subscription(row: Any, event: dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "")
    if event_type != str(row["event_type"]):
        return False
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return False
    entity_id = row["entity_id"]
    if entity_id and data.get("entity_id") != entity_id:
        return False
    expected_data = _decoded(row, "event_data_json")
    if expected_data is not None and not _event_data_matches(data, expected_data):
        return False
    if event_type != "state_changed":
        return True

    old_state = data.get("old_state") or {}
    new_state = data.get("new_state") or {}
    if not isinstance(old_state, dict) or not isinstance(new_state, dict):
        return False
    if row["from_state"] is not None and str(old_state.get("state")) != str(row["from_state"]):
        return False
    if row["to_state"] is not None and str(new_state.get("state")) != str(row["to_state"]):
        return False
    operator = row["operator"]
    if operator:
        actual: Any = new_state.get("state")
        if row["attribute"]:
            actual = _nested_value(new_state.get("attributes") or {}, str(row["attribute"]))
        try:
            return compare_values(actual, str(operator), _decoded(row, "expected_value_json"))
        except (TypeError, ValueError):
            return False
    return True


class EventEngine:
    def __init__(self, agent: Any, *, runtime_log: RuntimeLogger) -> None:
        self.agent = agent
        self.memory = agent.memory
        self.runtime_log = runtime_log

    async def dispatch(self, event: dict[str, Any]) -> int:
        queued = 0
        for row in list_subscriptions(self.memory.conn, enabled_only=True):
            if not event_matches_subscription(row, event):
                continue
            trigger_number = claim_trigger(self.memory.conn, int(row["id"]))
            if trigger_number is None:
                continue
            event_type = str(event.get("event_type") or "")
            data = event.get("data") or {}
            entity_id = str(data.get("entity_id") or "") or None
            try:
                task_id = self.memory.add_task(
                    run_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    title=f"Evento: {row['title']}",
                    instruction=str(row["instruction"]),
                    cancellation_key=(
                        f"event-listener-{int(row['id'])}-trigger-{trigger_number}"
                    ),
                    priority=int(row["priority"] or 50),
                )
                add_trigger_run(
                    self.memory.conn,
                    subscription_id=int(row["id"]),
                    event_type=event_type,
                    entity_id=entity_id,
                    event=event,
                    task_id=task_id,
                )
                queued += 1
                self.runtime_log(
                    "info",
                    "event_engine",
                    "trigger_queued",
                    subscription_id=int(row["id"]),
                    task_id=task_id,
                    event_type=event_type,
                    entity_id=entity_id,
                )
            except Exception as exc:
                add_trigger_run(
                    self.memory.conn,
                    subscription_id=int(row["id"]),
                    event_type=event_type,
                    entity_id=entity_id,
                    event=event,
                    error=str(exc),
                )
                self.runtime_log(
                    "error",
                    "event_engine",
                    "trigger_failed",
                    subscription_id=int(row["id"]),
                    error=str(exc),
                )
        return queued

    async def _connected_loop(self, stop: asyncio.Event) -> None:
        try:
            import websockets
        except ModuleNotFoundError as exc:
            raise RuntimeError("websockets no esta disponible") from exc
        token = getattr(self.agent, "ha_token", None)
        if not token:
            raise ValueError("Falta HA_TOKEN para el motor de eventos")
        async with websockets.connect(
            ha_websocket_url(self.agent),
            open_timeout=15,
            close_timeout=5,
            ping_interval=30,
            ping_timeout=20,
        ) as websocket:
            auth_required = json.loads(await websocket.recv())
            if auth_required.get("type") != "auth_required":
                raise RuntimeError("Home Assistant no solicito autenticacion WebSocket")
            await websocket.send(json.dumps({"type": "auth", "access_token": token}))
            auth_result = json.loads(await websocket.recv())
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(
                    f"Autenticacion WebSocket fallida: {auth_result.get('message') or auth_result.get('type')}"
                )
            await websocket.send(json.dumps({"id": 1, "type": "subscribe_events"}))
            subscribed = json.loads(await websocket.recv())
            if subscribed.get("id") != 1 or not subscribed.get("success"):
                raise RuntimeError(f"No se pudo suscribir al bus de eventos: {subscribed}")
            self.runtime_log("info", "event_engine", "connected")
            while not stop.is_set():
                receive = asyncio.create_task(websocket.recv())
                stopped = asyncio.create_task(stop.wait())
                done, pending = await asyncio.wait(
                    {receive, stopped}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stopped in done and stopped.result():
                    return
                message = json.loads(receive.result())
                if message.get("type") == "event" and isinstance(message.get("event"), dict):
                    await self.dispatch(message["event"])

    async def run(self, stop: asyncio.Event) -> None:
        retry_seconds = 1.0
        self.runtime_log("info", "event_engine", "started")
        while not stop.is_set():
            try:
                await self._connected_loop(stop)
                retry_seconds = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.runtime_log(
                    "warn",
                    "event_engine",
                    "disconnected",
                    error=str(exc),
                    retry_seconds=retry_seconds,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=retry_seconds)
                retry_seconds = min(retry_seconds * 2, 60.0)
