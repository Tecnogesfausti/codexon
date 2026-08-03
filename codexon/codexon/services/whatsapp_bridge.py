"""Canal interno y permanente entre WhatsApp (Baileys) y Codexon.

El proceso Node se ejecuta como hijo de Codexon. Ambos intercambian JSON Lines
por stdin/stdout, sin HTTP, MQTT, puertos ni credenciales internas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class CodexonConversation(Protocol):
    async def ask(
        self,
        user_text: str,
        task: str = "homeassistant",
        preferred_model: str | None = None,
        budget_mode: str | None = None,
    ) -> str: ...

    def estimate_request_budget(
        self, user_text: str, *, economy: bool = False
    ) -> dict[str, Any]: ...

    def total_usage_cost_usd(self) -> float: ...


LogFunction = Callable[..., None]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def parse_allowed_senders(value: str) -> frozenset[str]:
    if not value.strip():
        return frozenset()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value.split(",")
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return frozenset()
    return frozenset(normalize_sender(item) for item in parsed if normalize_sender(item))


def normalize_sender(value: Any) -> str:
    sender = str(value or "").strip()
    if "@" in sender:
        sender = sender.split("@", 1)[0]
    return "".join(character for character in sender if character.isdigit())


def fold_wake_word(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    ).strip()


def parse_wake_words(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value.split("|")
    if isinstance(parsed, str):
        parsed = parsed.split("|")
    if not isinstance(parsed, list):
        return ()

    wake_words: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        wake_word = str(item or "").strip()
        folded = fold_wake_word(wake_word)
        if wake_word and folded and folded not in seen:
            wake_words.append(wake_word)
            seen.add(folded)
    return tuple(wake_words)


def budget_reply_action(value: str) -> str | None:
    folded = fold_wake_word(value).strip(" .!?¿¡")
    if folded in {"vamos", "adelante", "hazlo", "ejecuta", "si", "vale", "ok"}:
        return "execute"
    if folded in {
        "mas barato", "algo mas barato", "busca algo mas barato",
        "busco algo mas barato", "barato", "economico", "modo economico",
    }:
        return "economy"
    if folded in {"cancela", "cancelar", "no", "dejalo", "para"}:
        return "cancel"
    return None


def format_budget_cents(value: Any) -> str:
    if value is None:
        return "coste desconocido"
    amount = float(value)
    if amount == 0:
        return "0"
    if amount < 0.001:
        return "<0,001"
    return f"{amount:.4f}".replace(".", ",").rstrip("0").rstrip(",")


def format_budget_preview(estimate: dict[str, Any], *, cheaper: bool = False) -> str:
    title = "Opcion economica" if cheaper else "Presupuesto previo"
    lines = [f"{title}:"]
    for index, stage in enumerate(estimate.get("stages") or [], start=1):
        suffix = " (si es estadistica)" if stage.get("conditional") else ""
        cost = (
            None
            if stage.get("estimated_cost_usd") is None
            else float(stage["estimated_cost_usd"]) * 100
        )
        lines.append(
            f"{index}. {stage.get('label')}{suffix}: {stage.get('model')} "
            f"≈ {format_budget_cents(cost)} centimos USD"
        )
    total = estimate.get("estimated_cost_cents_usd")
    lines.append(f"Total estimado: {format_budget_cents(total)} centimos USD.")
    if estimate.get("within_threshold") is True:
        lines.append("Esta por debajo del umbral de 0,2 centimos.")
    elif estimate.get("within_threshold") is False:
        lines.append("Supera el umbral de 0,2 centimos.")
    else:
        lines.append("Falta precio de catalogo para una etapa; el total no es completo.")
    lines.append(
        "Responde «vamos» para ejecutar, «mas barato» para recalcular en modo economico "
        "o «cancelar»."
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class WhatsAppBridgeConfig:
    enabled: bool
    node_binary: str
    bridge_path: Path
    data_dir: Path
    allowed_senders: frozenset[str]
    wake_words: tuple[str, ...]
    reject_calls: bool
    restart_seconds: float = 5.0
    max_pending_messages: int = 50

    @classmethod
    def from_env(cls) -> "WhatsAppBridgeConfig":
        wake_words_value = os.getenv("CODEXON_WHATSAPP_WAKE_WORDS", "").strip()
        return cls(
            enabled=env_bool("CODEXON_WHATSAPP_ENABLED"),
            node_binary=os.getenv("CODEXON_WHATSAPP_NODE", "node").strip() or "node",
            bridge_path=Path(
                os.getenv(
                    "CODEXON_WHATSAPP_BRIDGE",
                    str(Path(__file__).resolve().parents[1] / "whatsapp-core" / "bridge.mjs"),
                )
            ).expanduser(),
            data_dir=Path(
                os.getenv("CODEXON_WHATSAPP_DATA_DIR", "/data/codexon/whatsapp")
            ).expanduser(),
            allowed_senders=parse_allowed_senders(
                os.getenv("CODEXON_WHATSAPP_ALLOWED_SENDERS", "")
            ),
            wake_words=parse_wake_words(wake_words_value),
            reject_calls=env_bool("CODEXON_WHATSAPP_REJECT_CALLS", True),
        )


class WhatsAppBridge:
    def __init__(
        self,
        agent: CodexonConversation,
        *,
        config: WhatsAppBridgeConfig | None = None,
        log: LogFunction | None = None,
    ) -> None:
        self.agent = agent
        self.config = config or WhatsAppBridgeConfig.from_env()
        self.log = log or (lambda *_args, **_kwargs: None)
        self.process: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()
        self._messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=self.config.max_pending_messages
        )
        self._budget_path = self.config.data_dir / "pending-budgets.json"

    def _read_pending_budgets(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self._budget_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        cutoff = time.time() - 86400
        return {
            str(sender): dict(record)
            for sender, record in payload.items()
            if isinstance(record, dict)
            and float(record.get("created_at") or 0) >= cutoff
        }

    def _write_pending_budgets(self, pending: dict[str, dict[str, Any]]) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._budget_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(pending, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self._budget_path)

    def _pending_budget(self, sender: str) -> dict[str, Any] | None:
        return self._read_pending_budgets().get(sender)

    def _save_pending_budget(self, sender: str, record: dict[str, Any]) -> None:
        pending = self._read_pending_budgets()
        pending[sender] = record
        self._write_pending_budgets(pending)

    def _pop_pending_budget(self, sender: str) -> dict[str, Any] | None:
        pending = self._read_pending_budgets()
        record = pending.pop(sender, None)
        self._write_pending_budgets(pending)
        return record

    def _read_data_file(self, name: str) -> dict[str, Any]:
        try:
            value = json.loads(
                (self.config.data_dir / name).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def list_contacts(
        self, *, query: str = "", limit: int = 50
    ) -> list[dict[str, str]]:
        raw_contacts = self._read_data_file("contacts.json").get("contacts") or []
        folded_query = query.strip().casefold()
        contacts: list[dict[str, str]] = []
        for raw in raw_contacts:
            if not isinstance(raw, dict):
                continue
            contact = {
                "id": str(raw.get("id") or "").strip(),
                "name": str(raw.get("name") or "").strip(),
                "phone": str(raw.get("phone") or "").strip(),
                "notify": str(raw.get("notify") or "").strip(),
                "verified_name": str(raw.get("verifiedName") or "").strip(),
            }
            if not contact["id"]:
                continue
            haystack = " ".join(contact.values()).casefold()
            if folded_query and folded_query not in haystack:
                continue
            contacts.append(contact)
        contacts.sort(key=lambda item: (item["name"].casefold(), item["id"]))
        return contacts[: max(1, min(int(limit), 200))]

    def channel_status(self) -> dict[str, Any]:
        stored = self._read_data_file("status.json")
        last_message = stored.get("lastMessage")
        return {
            "enabled": self.config.enabled,
            "state": str(stored.get("state") or "unknown"),
            "connected": stored.get("state") == "connected",
            "message_count": int(stored.get("messageCount") or 0),
            "last_message": last_message if isinstance(last_message, dict) else None,
            "updated_at": stored.get("updatedAt"),
            "contacts": len(self.list_contacts(limit=200)),
        }

    def recent_messages(
        self, *, limit: int = 20, direction: str = "all"
    ) -> list[dict[str, Any]]:
        raw_messages = self._read_data_file("messages.json").get("messages") or []
        normalized_direction = direction.strip().casefold() or "all"
        if normalized_direction not in {"all", "incoming", "outgoing"}:
            raise ValueError("direction debe ser all, incoming u outgoing")
        messages = [
            message
            for message in raw_messages
            if isinstance(message, dict)
            and (
                normalized_direction == "all"
                or message.get("direction") == normalized_direction
            )
        ]
        return messages[-max(1, min(int(limit), 100)) :]

    def _last_recipient(self) -> str:
        stored = self._read_data_file("status.json")
        recipient = str(
            (stored.get("lastMessage") or {}).get("from") or ""
        ).strip()
        if recipient:
            return recipient
        contacts = self._read_data_file("contacts.json").get("contacts") or []
        if not contacts:
            return ""
        return str((contacts[-1] or {}).get("id") or "").strip()

    def _resolve_recipient(self, value: str) -> str:
        recipient = str(value or "last_sender").strip()
        if recipient.casefold() in {
            "last_sender",
            "ultimo",
            "último",
            "me",
            "self",
        }:
            return self._last_recipient()
        contacts = self.list_contacts(limit=200)
        folded = recipient.casefold()
        digits = normalize_sender(recipient)
        exact = [
            contact
            for contact in contacts
            if recipient == contact["id"]
            or (digits and digits == contact["phone"])
            or folded
            in {
                contact["name"].casefold(),
                contact["notify"].casefold(),
                contact["verified_name"].casefold(),
            }
        ]
        if len(exact) == 1:
            return exact[0]["id"]
        partial = [
            contact
            for contact in contacts
            if folded
            and folded
            in " ".join(
                (
                    contact["name"],
                    contact["notify"],
                    contact["verified_name"],
                )
            ).casefold()
        ]
        if len(partial) == 1:
            return partial[0]["id"]
        if len(exact) > 1 or len(partial) > 1:
            raise ValueError("destinatario WhatsApp ambiguo; usa id o teléfono")
        return recipient

    async def send_message(self, message: str, to: str = "last_sender") -> None:
        text = str(message or "").strip()
        if not text:
            raise ValueError("mensaje WhatsApp vacío")
        recipient = self._resolve_recipient(to)
        if not recipient:
            raise ValueError("no hay un destinatario WhatsApp reciente")
        await self._send(
            {
                "type": "send",
                "to": recipient,
                "message": text,
            }
        )

    async def run(self, stop: asyncio.Event) -> None:
        if not self.config.enabled:
            return
        if not self.config.bridge_path.is_file():
            self._log(
                "error",
                "nucleo WhatsApp no encontrado",
                path=str(self.config.bridge_path),
            )
            return

        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        while not stop.is_set():
            try:
                await self._run_once(stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - el supervisor debe sobrevivir
                self._log("error", "fallo del nucleo WhatsApp", error=str(exc))
            if not stop.is_set():
                self._log(
                    "warn",
                    "reiniciando nucleo WhatsApp",
                    delay_seconds=self.config.restart_seconds,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.config.restart_seconds
                    )

    async def _run_once(self, stop: asyncio.Event) -> None:
        child_env = os.environ.copy()
        child_env.update(
            {
                "CODEXON_WHATSAPP_DATA_DIR": str(self.config.data_dir),
                "CODEXON_WHATSAPP_REJECT_CALLS": (
                    "true" if self.config.reject_calls else "false"
                ),
            }
        )
        self.process = await asyncio.create_subprocess_exec(
            self.config.node_binary,
            str(self.config.bridge_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
        self._log("info", "nucleo WhatsApp iniciado", pid=self.process.pid)

        reader = asyncio.create_task(self._read_protocol())
        stderr = asyncio.create_task(self._read_stderr())
        worker = asyncio.create_task(self._process_messages())
        child_done = asyncio.create_task(self.process.wait())
        stop_requested = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {child_done, stop_requested}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_requested in done and stop.is_set():
                await self._terminate_child()
            else:
                code = child_done.result()
                self._log("warn", "nucleo WhatsApp finalizado", returncode=code)
        finally:
            stop_requested.cancel()
            child_done.cancel()
            reader.cancel()
            stderr.cancel()
            worker.cancel()
            await asyncio.gather(
                stop_requested,
                child_done,
                reader,
                stderr,
                worker,
                return_exceptions=True,
            )
            self.process = None

    async def _read_protocol(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._log("warn", "linea invalida del nucleo WhatsApp")
                continue
            event_type = event.get("type")
            if event_type == "message":
                if not self._accept_message(event):
                    continue
                try:
                    self._messages.put_nowait(event)
                except asyncio.QueueFull:
                    self._log(
                        "warn",
                        "cola WhatsApp llena; mensaje descartado",
                        message_id=event.get("id"),
                    )
            elif event_type == "status":
                self._log(
                    "info",
                    "estado WhatsApp",
                    state=event.get("state"),
                    has_qr=bool(event.get("hasQr")),
                )
            elif event_type == "send_result" and not event.get("ok"):
                self._log(
                    "error",
                    "no se pudo responder por WhatsApp",
                    error=event.get("error"),
                )

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            message = line.decode("utf-8", errors="replace").strip()
            if message:
                self._log("info", "WhatsApp/Baileys", detail=message[:2000])

    def _accept_message(self, event: dict[str, Any]) -> bool:
        if event.get("source") != "notify":
            return False
        sender_jid = str(event.get("from") or "")
        sender = normalize_sender(sender_jid)
        if self.config.allowed_senders and sender not in self.config.allowed_senders:
            self._log("warn", "remitente WhatsApp no permitido", sender=sender_jid)
            return False
        body = str(event.get("body") or "").strip()
        if not body:
            return False
        pending_budget = self._pending_budget(sender_jid)
        direct_budget_action = budget_reply_action(body)
        if pending_budget and direct_budget_action:
            event["budget_reply"] = direct_budget_action
            return True
        wake_words = self.config.wake_words
        if wake_words:
            matched_length = 0
            budget_preview = False
            for wake_word in sorted(wake_words, key=len, reverse=True):
                candidate_budget_preview = False
                candidate = body[: len(wake_word)]
                if fold_wake_word(candidate) != fold_wake_word(wake_word):
                    continue
                end = len(wake_word)
                if body[end : end + 1] == "$":
                    candidate_budget_preview = True
                    end += 1
                if len(body) > end:
                    separator = body[end]
                    if not (separator.isspace() or separator in ":,-"):
                        continue
                matched_length = end
                budget_preview = candidate_budget_preview
                break
            if not matched_length:
                return False
            body = body[matched_length:].lstrip(" :,-")
            if not body:
                return False
            event["body"] = body
            if budget_preview:
                event["budget_preview"] = True
            elif pending_budget:
                action = budget_reply_action(body)
                if action:
                    event["budget_reply"] = action
        return True

    async def _process_messages(self) -> None:
        while True:
            event = await self._messages.get()
            if event is None:
                return
            recipient = str(event.get("from") or "")
            message_id = str(event.get("id") or "")
            try:
                sender_name = str(event.get("pushName") or recipient).strip()
                body = str(event.get("body") or "").strip()
                request = (
                    "[Contexto del canal: mensaje entrante de WhatsApp de "
                    f"{sender_name} ({recipient}). Tu respuesta final se enviará "
                    "automáticamente a este mismo chat; no uses "
                    "whatsapp_send_message para responder a este mensaje.]\n\n"
                    f"{body}"
                )
                if event.get("budget_preview"):
                    estimate = self.agent.estimate_request_budget(body, economy=False)
                    self._save_pending_budget(
                        recipient,
                        {
                            "created_at": time.time(),
                            "request": body,
                            "mode": "normal",
                            "estimate": estimate,
                        },
                    )
                    answer = format_budget_preview(estimate)
                elif event.get("budget_reply"):
                    action = str(event["budget_reply"])
                    pending = self._pending_budget(recipient)
                    if not pending:
                        answer = "Ese presupuesto ya no esta pendiente. Envia una nueva orden con casa$."
                    elif action == "cancel":
                        self._pop_pending_budget(recipient)
                        answer = "Presupuesto cancelado; no he ejecutado la peticion."
                    elif action == "economy":
                        estimate = self.agent.estimate_request_budget(
                            str(pending["request"]), economy=True
                        )
                        pending.update(
                            created_at=time.time(), mode="economy", estimate=estimate
                        )
                        self._save_pending_budget(recipient, pending)
                        answer = format_budget_preview(estimate, cheaper=True)
                    else:
                        pending = self._pop_pending_budget(recipient) or pending
                        original = str(pending["request"])
                        execution_request = (
                            "[Contexto del canal: mensaje entrante de WhatsApp de "
                            f"{sender_name} ({recipient}). Tu respuesta final se enviará "
                            "automáticamente a este mismo chat; no uses whatsapp_send_message "
                            "para responder a este mensaje.]\n\n"
                            f"{original}"
                        )
                        before_cost = self.agent.total_usage_cost_usd()
                        answer = await self.agent.ask(
                            execution_request,
                            task="homeassistant",
                            budget_mode=str(pending.get("mode") or "normal"),
                        )
                        actual_cents = max(
                            0.0,
                            (self.agent.total_usage_cost_usd() - before_cost) * 100,
                        )
                        estimated_cents = (pending.get("estimate") or {}).get(
                            "estimated_cost_cents_usd"
                        )
                        answer += (
                            "\n\nCoste LLM real: "
                            f"{format_budget_cents(actual_cents)} centimos USD"
                            + (
                                f"; estimado: {format_budget_cents(estimated_cents)}."
                                if estimated_cents is not None
                                else "."
                            )
                        )
                else:
                    answer = await self.agent.ask(
                        request, task="homeassistant"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - hay que contestar al usuario
                self._log(
                    "error",
                    "Codexon no pudo procesar mensaje WhatsApp",
                    message_id=message_id,
                    error=str(exc),
                )
                answer = f"Codexon no pudo procesar el mensaje: {exc}"
            for chunk in split_message(answer):
                await self._send(
                    {
                        "type": "send",
                        "to": recipient,
                        "message": chunk,
                        "replyTo": message_id,
                    }
                )

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if not process or not process.stdin or process.returncode is not None:
            raise RuntimeError("el nucleo WhatsApp no esta disponible")
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        async with self._write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def _terminate_child(self) -> None:
        process = self.process
        if not process or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=8)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    def _log(self, level: str, message: str, **fields: Any) -> None:
        self.log(level, "whatsapp", message, **fields)


def split_message(value: Any, limit: int = 3500) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return ["Codexon ha terminado sin generar una respuesta."]
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = text.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks
