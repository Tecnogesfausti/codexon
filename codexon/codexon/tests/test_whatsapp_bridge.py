import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from services.whatsapp_bridge import (
    WhatsAppBridge,
    WhatsAppBridgeConfig,
    normalize_sender,
    parse_allowed_senders,
    parse_wake_words,
    split_message,
)


def config(**overrides):
    values = {
        "enabled": True,
        "node_binary": "node",
        "bridge_path": Path("/tmp/bridge.mjs"),
        "data_dir": Path("/tmp/codexon-whatsapp-test"),
        "allowed_senders": frozenset(),
        "wake_words": (),
        "reject_calls": True,
    }
    values.update(overrides)
    return WhatsAppBridgeConfig(**values)


class DummyAgent:
    async def ask(self, user_text, task="homeassistant", preferred_model=None):
        return f"respuesta: {user_text}"


class BudgetAgent:
    def __init__(self):
        self.requests = []
        self.cost = 1.0

    def estimate_request_budget(self, user_text, *, economy=False):
        cents = 0.08 if economy else 0.35
        return {
            "request": user_text,
            "mode": "economy" if economy else "normal",
            "stages": [
                {
                    "label": "Clasificar la peticion",
                    "model": "google/gemini-2.5-flash-lite" if economy else "deepseek/model",
                    "estimated_cost_usd": cents / 100,
                    "conditional": False,
                }
            ],
            "estimated_cost_cents_usd": cents,
            "within_threshold": cents < 0.2,
        }

    def total_usage_cost_usd(self):
        return self.cost

    async def ask(
        self,
        user_text,
        task="homeassistant",
        preferred_model=None,
        budget_mode=None,
    ):
        self.requests.append((user_text, budget_mode))
        self.cost += 0.0007
        return "escucha creada"


class WhatsAppBridgeTests(unittest.TestCase):
    def test_sender_normalization_and_list(self):
        self.assertEqual(normalize_sender("+34 600 123 123@s.whatsapp.net"), "34600123123")
        self.assertEqual(
            parse_allowed_senders('["+34 600 123 123", "34600999888@s.whatsapp.net"]'),
            frozenset({"34600123123", "34600999888"}),
        )

    def test_empty_allowlist_accepts_private_incoming_message(self):
        bridge = WhatsAppBridge(DummyAgent(), config=config())
        event = {
            "type": "message",
            "source": "notify",
            "from": "34600123123@s.whatsapp.net",
            "fromMe": False,
            "body": "enciende la luz",
        }
        self.assertTrue(bridge._accept_message(event))

    def test_filters_history_and_non_allowed_sender(self):
        bridge = WhatsAppBridge(
            DummyAgent(),
            config=config(allowed_senders=frozenset({"34600123123"})),
        )
        base = {
            "type": "message",
            "source": "notify",
            "from": "34600123123@s.whatsapp.net",
            "fromMe": False,
            "body": "estado",
        }
        self.assertFalse(bridge._accept_message({**base, "source": "history"}))
        self.assertFalse(
            bridge._accept_message(
                {**base, "from": "34600999999@s.whatsapp.net"}
            )
        )

    def test_accepts_own_and_group_messages_without_separate_flags(self):
        bridge = WhatsAppBridge(DummyAgent(), config=config())
        base = {
            "type": "message",
            "source": "notify",
            "from": "34600123123@s.whatsapp.net",
            "fromMe": False,
            "body": "estado",
        }
        self.assertTrue(bridge._accept_message({**base, "fromMe": True}))
        self.assertTrue(
            bridge._accept_message({**base, "from": "120363000@g.us"})
        )

    def test_single_wake_word_is_removed(self):
        bridge = WhatsAppBridge(
            DummyAgent(), config=config(wake_words=("Codexon",))
        )
        event = {
            "source": "notify",
            "from": "34600123123@s.whatsapp.net",
            "fromMe": False,
            "body": "codexon: dime el estado",
        }
        self.assertTrue(bridge._accept_message(event))
        self.assertEqual(event["body"], "dime el estado")

    def test_multiple_wake_words_are_removed(self):
        bridge = WhatsAppBridge(
            DummyAgent(),
            config=config(wake_words=parse_wake_words("casa|huérto|CASA")),
        )
        for body in ("Casa dime la temperatura", "HUERTO: riega los bonsais"):
            with self.subTest(body=body):
                event = {
                    "source": "notify",
                    "from": "34600123123@s.whatsapp.net",
                    "fromMe": False,
                    "body": body,
                }
                self.assertTrue(bridge._accept_message(event))
                self.assertNotIn(
                    event["body"].split(maxsplit=1)[0].casefold(),
                    {"casa", "huerto"},
                )

    def test_wake_word_requires_a_boundary_and_a_command(self):
        bridge = WhatsAppBridge(
            DummyAgent(), config=config(wake_words=parse_wake_words("casa|huerto"))
        )
        base = {
            "source": "notify",
            "from": "34600123123@s.whatsapp.net",
            "fromMe": False,
        }
        self.assertFalse(
            bridge._accept_message({**base, "body": "casamiento mañana"})
        )
        self.assertFalse(bridge._accept_message({**base, "body": "Codexon estado"}))
        self.assertFalse(bridge._accept_message({**base, "body": "casa"}))

    def test_dollar_after_wake_word_requests_budget_preview(self):
        bridge = WhatsAppBridge(
            DummyAgent(), config=config(wake_words=("casa",))
        )
        event = {
            "source": "notify",
            "from": "34600123123@s.whatsapp.net",
            "body": "casa$ cuando haga 25 grados avisa",
        }

        self.assertTrue(bridge._accept_message(event))
        self.assertTrue(event["budget_preview"])
        self.assertEqual(event["body"], "cuando haga 25 grados avisa")

    def test_splits_long_answers(self):
        chunks = split_message(("texto " * 1500).strip(), limit=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))

    def test_lists_contacts_status_and_recent_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "contacts.json").write_text(
                json.dumps(
                    {
                        "contacts": [
                            {
                                "id": "34600123123@s.whatsapp.net",
                                "name": "Martinson",
                                "phone": "34600123123",
                                "notify": "Martinson",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "status.json").write_text(
                json.dumps(
                    {
                        "state": "connected",
                        "messageCount": 2,
                        "lastMessage": {"body": "estado"},
                        "updatedAt": "2026-07-29T11:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "messages.json").write_text(
                json.dumps(
                    {
                        "messages": [
                            {"direction": "incoming", "body": "estado"},
                            {"direction": "outgoing", "body": "todo bien"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            bridge = WhatsAppBridge(
                DummyAgent(),
                config=config(data_dir=data_dir),
            )
            self.assertEqual(bridge.list_contacts(query="mart")[0]["name"], "Martinson")
            self.assertEqual(bridge.channel_status()["contacts"], 1)
            self.assertTrue(bridge.channel_status()["connected"])
            self.assertEqual(
                bridge.recent_messages(direction="outgoing")[0]["body"],
                "todo bien",
            )
            self.assertEqual(
                bridge._resolve_recipient("Martinson"),
                "34600123123@s.whatsapp.net",
            )


class WhatsAppBridgeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_preview_economy_and_execution_survive_as_separate_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = BudgetAgent()
            bridge = WhatsAppBridge(
                agent,
                config=config(
                    data_dir=Path(directory),
                    wake_words=("casa",),
                ),
            )
            sent = []

            async def capture(payload):
                sent.append(payload)

            bridge._send = capture

            preview = {
                "id": "budget-1",
                "source": "notify",
                "from": "34600123123@s.whatsapp.net",
                "body": "casa$ cuando haga 25 grados avisa",
            }
            self.assertTrue(bridge._accept_message(preview))
            await bridge._messages.put(preview)
            await bridge._messages.put(None)
            await bridge._process_messages()
            self.assertIn("0,35 centimos USD", sent[-1]["message"])

            cheaper = {
                "id": "budget-2",
                "source": "notify",
                "from": "34600123123@s.whatsapp.net",
                "body": "mas barato",
            }
            self.assertTrue(bridge._accept_message(cheaper))
            await bridge._messages.put(cheaper)
            await bridge._messages.put(None)
            await bridge._process_messages()
            self.assertIn("Opcion economica", sent[-1]["message"])
            self.assertIn("0,08 centimos USD", sent[-1]["message"])

            restarted = WhatsAppBridge(
                agent,
                config=config(
                    data_dir=Path(directory),
                    wake_words=("casa",),
                ),
            )
            restarted._send = capture
            execute = {
                "id": "budget-3",
                "source": "notify",
                "from": "34600123123@s.whatsapp.net",
                "body": "vamos",
            }
            self.assertTrue(restarted._accept_message(execute))
            await restarted._messages.put(execute)
            await restarted._messages.put(None)
            await restarted._process_messages()

            self.assertEqual(agent.requests[-1][1], "economy")
            self.assertIn("cuando haga 25 grados avisa", agent.requests[-1][0])
            self.assertIn("Coste LLM real", sent[-1]["message"])

    async def test_sends_proactive_message_to_last_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "status.json").write_text(
                json.dumps(
                    {
                        "lastMessage": {
                            "from": "34600123123@s.whatsapp.net",
                        }
                    }
                ),
                encoding="utf-8",
            )
            bridge = WhatsAppBridge(
                DummyAgent(),
                config=config(data_dir=data_dir),
            )
            sent = []

            async def capture(payload):
                sent.append(payload)

            bridge._send = capture
            await bridge.send_message("Temperatura exterior: 25 °C")
            self.assertEqual(
                sent,
                [
                    {
                        "type": "send",
                        "to": "34600123123@s.whatsapp.net",
                        "message": "Temperatura exterior: 25 °C",
                    }
                ],
            )

    async def test_falls_back_to_persisted_contact_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "status.json").write_text(
                json.dumps({"lastMessage": None}),
                encoding="utf-8",
            )
            (data_dir / "contacts.json").write_text(
                json.dumps(
                    {
                        "contacts": [
                            {"id": "34600123123@s.whatsapp.net"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            bridge = WhatsAppBridge(
                DummyAgent(),
                config=config(data_dir=data_dir),
            )
            sent = []

            async def capture(payload):
                sent.append(payload)

            bridge._send = capture
            await bridge.send_message("Aviso")
            self.assertEqual(sent[0]["to"], "34600123123@s.whatsapp.net")

    async def test_processes_message_and_writes_reply(self):
        bridge = WhatsAppBridge(DummyAgent(), config=config())
        sent = []

        async def capture(payload):
            sent.append(payload)

        bridge._send = capture
        task = asyncio.create_task(bridge._process_messages())
        await bridge._messages.put(
            {
                "id": "message-1",
                "from": "34600123123@s.whatsapp.net",
                "body": "estado",
            }
        )
        await bridge._messages.put(None)
        await task
        self.assertEqual(sent[0]["to"], "34600123123@s.whatsapp.net")
        self.assertEqual(sent[0]["replyTo"], "message-1")
        self.assertIn("mensaje entrante de WhatsApp", sent[0]["message"])
        self.assertTrue(sent[0]["message"].endswith("estado"))
