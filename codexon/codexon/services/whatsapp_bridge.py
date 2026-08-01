"""Canal interno y permanente entre WhatsApp (Baileys) y Codexon.

El proceso Node se ejecuta como hijo de Codexon. Ambos intercambian JSON Lines
por stdin/stdout, sin HTTP, MQTT, puertos ni credenciales internas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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
    ) -> str: ...


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
        wake_words = self.config.wake_words
        if wake_words:
            matched_length = 0
            for wake_word in sorted(wake_words, key=len, reverse=True):
                candidate = body[: len(wake_word)]
                if fold_wake_word(candidate) != fold_wake_word(wake_word):
                    continue
                if len(body) > len(wake_word):
                    separator = body[len(wake_word)]
                    if not (separator.isspace() or separator in ":,-"):
                        continue
                matched_length = len(wake_word)
                break
            if not matched_length:
                return False
            body = body[matched_length:].lstrip(" :,-")
            if not body:
                return False
            event["body"] = body
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
                request = (
                    "[Contexto del canal: mensaje entrante de WhatsApp de "
                    f"{sender_name} ({recipient}). Tu respuesta final se enviará "
                    "automáticamente a este mismo chat; no uses "
                    "whatsapp_send_message para responder a este mensaje.]\n\n"
                    f"{event['body']}"
                )
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
