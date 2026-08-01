from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from site_profile import SiteProfile
from tools.homeassistant import (
    ha_call_service,
    ha_get_state,
    ha_get_tts_media_players,
    ha_search_entities,
    ha_search_site_entities,
    ha_send_mobile_alert,
)


class FakeResponse:
    def __init__(self, payload=None) -> None:
        self._payload = [] if payload is None else payload
        self.content = json.dumps(self._payload).encode()

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class FakeAsyncClient:
    posts: list[dict] = []
    states: list[dict] = []

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, headers: dict, json: dict):
        self.posts.append({"url": url, "headers": headers, "json": json})
        return FakeResponse()

    async def get(self, url: str, headers: dict, params: dict | None = None):
        marker = "/api/states/"
        if marker in url:
            entity_id = url.split(marker, 1)[1]
            for state in self.states:
                if state.get("entity_id") == entity_id:
                    return FakeResponse(state)
            return FakeResponse({})
        return FakeResponse(self.states)


class FakeHttpx:
    AsyncClient = FakeAsyncClient


def context(require_action_confirmation: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        ha_base_url="http://homeassistant:8123",
        ha_token="token",
        httpx=FakeHttpx,
        require_action_confirmation=require_action_confirmation,
        service_verify_attempts=1,
        service_verify_delay=0,
    )


def write_storage(root: Path, filename: str, key: str, rows: list[dict]) -> None:
    storage = root / ".storage"
    storage.mkdir(exist_ok=True)
    (storage / filename).write_text(json.dumps({"data": {key: rows}}), encoding="utf-8")


class HomeAssistantTTSTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsyncClient.posts.clear()
        FakeAsyncClient.states.clear()

    def test_site_entity_collection_returns_live_read_only_inventory(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.extend(
                [
                    {
                        "entity_id": "switch.riego_rele1",
                        "state": "off",
                        "attributes": {"friendly_name": "Riego 1"},
                    },
                    {
                        "entity_id": "switch.riego2_rele1",
                        "state": "on",
                        "attributes": {"friendly_name": "Riego 2"},
                    },
                ]
            )
            ctx = context()
            ctx.site_profile = SiteProfile(
                path=None,
                roles={
                    "irrigation.primary.master": {
                        "entity_id": "switch.riego_rele1",
                        "label": "relé maestro de riego",
                    },
                    "irrigation.huerta.switches": {
                        "entities": [
                            "switch.riego_rele1",
                            "switch.riego2_rele1",
                        ],
                        "label": "grifos de la huerta",
                        "aliases": ["grifos huerta"],
                        "area": "huerta",
                        "kind": "collection",
                    }
                },
            )

            payload = json.loads(
                await ha_search_site_entities(ctx, {"query": "grifos huerta"})
            )

            self.assertTrue(payload["read_only"])
            self.assertEqual(payload["count"], 2)
            self.assertFalse(payload["simultaneous_limit_known"])
            self.assertIsNone(payload["max_simultaneous"])
            self.assertIn("no define", payload["constraint_note"])
            self.assertEqual(
                [row["entity_id"] for row in payload["entities"]],
                ["switch.riego2_rele1", "switch.riego_rele1"],
            )
            self.assertEqual(payload["entities"][0]["state"], "on")
            master = next(
                row
                for row in payload["entities"]
                if row["entity_id"] == "switch.riego_rele1"
            )
            self.assertEqual(master["specific_role"], "irrigation.primary.master")
            self.assertEqual(master["label"], "relé maestro de riego")
            self.assertTrue(master["semantic_name_known"])
            unassigned = next(
                row
                for row in payload["entities"]
                if row["entity_id"] == "switch.riego2_rele1"
            )
            self.assertFalse(unassigned["semantic_name_known"])
            self.assertIn("Sin zona", unassigned["assignment_note"])

        asyncio.run(run())

    def test_tts_does_not_require_extra_confirmation_when_user_asked_to_speak(self) -> None:
        async def run() -> None:
            result = await ha_call_service(
                context(require_action_confirmation=True),
                {
                    "domain": "tts",
                    "service": "google_translate_say",
                    "service_data": {
                        "cache": False,
                        "language": "es",
                        "entity_id": "media_player.salon",
                        "message": "Hola",
                    },
                },
            )
            self.assertIn('"called":true', result)

        asyncio.run(run())

    def test_tts_media_players_include_off_and_exclude_unavailable(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.extend(
                [
                    {
                        "entity_id": "media_player.nesthubdea6",
                        "state": "off",
                        "attributes": {"friendly_name": "Cuarto de estar", "volume_level": 0.66, "is_volume_muted": False},
                    },
                    {
                        "entity_id": "media_player.mudo",
                        "state": "idle",
                        "attributes": {"friendly_name": "Altavoz mudo", "volume_level": 0.0, "is_volume_muted": True},
                    },
                    {
                        "entity_id": "media_player.altavoz_roto",
                        "state": "unavailable",
                        "attributes": {"friendly_name": "Altavoz roto"},
                    },
                ]
            )
            result = await ha_get_tts_media_players(context(), {"limit": 100})
            self.assertIn("media_player.nesthubdea6", result)
            self.assertIn('"state":"off"', result)
            self.assertIn('"available_for_tts":true', result)
            self.assertNotIn("media_player.mudo", result)
            self.assertNotIn("media_player.altavoz_roto", result)

        asyncio.run(run())

    def test_tts_requires_google_translate_contract(self) -> None:
        invalid_payloads = [
            ({"cache": True, "language": "es", "entity_id": "media_player.salon", "message": "Hola"}, "cache=false"),
            ({"cache": False, "language": "en", "entity_id": "media_player.salon", "message": "Hola"}, "language=es"),
            ({"cache": False, "language": "es", "entity_id": "light.salon", "message": "Hola"}, "media_player"),
            ({"cache": False, "language": "es", "entity_id": "media_player.salon", "message": ""}, "message"),
        ]

        async def run() -> None:
            for service_data, expected in invalid_payloads:
                with self.subTest(service_data=json.dumps(service_data, sort_keys=True)):
                    with self.assertRaisesRegex(ValueError, expected):
                        await ha_call_service(
                            context(require_action_confirmation=False),
                            {
                                "domain": "tts",
                                "service": "google_translate_say",
                                "service_data": service_data,
                                "confirm": True,
                            },
                        )

        asyncio.run(run())

    def test_valid_tts_call_posts_expected_payload(self) -> None:
        async def run() -> None:
            result = await ha_call_service(
                context(require_action_confirmation=False),
                {
                    "domain": "tts",
                    "service": "google_translate_say",
                    "service_data": {
                        "cache": False,
                        "language": "es",
                        "entity_id": "media_player.salon",
                        "message": "Hola",
                    },
                    "confirm": True,
                },
            )
            self.assertIn('"called":true', result)
            self.assertEqual(len(FakeAsyncClient.posts), 1)
            self.assertEqual(FakeAsyncClient.posts[0]["url"], "http://homeassistant:8123/api/services/tts/google_translate_say")
            self.assertEqual(
                FakeAsyncClient.posts[0]["json"],
                {
                    "cache": False,
                    "language": "es",
                    "entity_id": "media_player.salon",
                    "message": "Hola",
                },
            )

        asyncio.run(run())

    def test_tts_defaults_cache_false_and_spanish_language(self) -> None:
        async def run() -> None:
            await ha_call_service(
                context(require_action_confirmation=True),
                {
                    "domain": "tts",
                    "service": "google_translate_say",
                    "service_data": {
                        "entity_id": "media_player.salon",
                        "message": "Hola",
                    },
                },
            )
            self.assertEqual(FakeAsyncClient.posts[0]["json"]["cache"], False)
            self.assertEqual(FakeAsyncClient.posts[0]["json"]["language"], "es")

        asyncio.run(run())

    def test_tts_resolves_friendly_name_slug_to_real_media_player(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.extend(
                [
                    {
                        "entity_id": "media_player.nesthubdea6",
                        "state": "off",
                        "attributes": {"friendly_name": "Cuarto de estar"},
                    },
                    {
                        "entity_id": "media_player.altavoz_roto",
                        "state": "unavailable",
                        "attributes": {"friendly_name": "Altavoz roto"},
                    },
                ]
            )
            await ha_call_service(
                context(require_action_confirmation=True),
                {
                    "domain": "tts",
                    "service": "google_translate_say",
                    "service_data": {
                        "entity_id": "media_player.cuarto_de_estar",
                        "message": "Hola",
                    },
                    "target": {"entity_id": "media_player.cuarto_de_estar"},
                },
            )
            self.assertEqual(FakeAsyncClient.posts[0]["json"]["entity_id"], "media_player.nesthubdea6")
            self.assertNotIn("target", FakeAsyncClient.posts[0]["json"])

        asyncio.run(run())

    def test_tts_rejects_muted_or_zero_volume_target(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.append(
                {
                    "entity_id": "media_player.automatismosf2_audioauto_mp",
                    "state": "idle",
                    "attributes": {
                        "friendly_name": "automatismosf2 audioauto_mp",
                        "volume_level": 0.0,
                        "is_volume_muted": True,
                    },
                }
            )
            with self.assertRaisesRegex(ValueError, "Destino TTS no audible"):
                await ha_call_service(
                    context(require_action_confirmation=True),
                    {
                        "domain": "tts",
                        "service": "google_translate_say",
                        "service_data": {
                            "entity_id": "media_player.automatismosf2_audioauto_mp",
                            "message": "Hola",
                        },
                    },
                )
            self.assertEqual(FakeAsyncClient.posts, [])

        asyncio.run(run())

    def test_search_entities_matches_accents_and_plural_natural_name(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.extend(
                [
                    {
                        "entity_id": "sensor.salon_temperatura",
                        "state": "23.1",
                        "attributes": {
                            "friendly_name": "Salón temperatura",
                            "device_class": "temperature",
                            "unit_of_measurement": "°C",
                        },
                    },
                    {
                        "entity_id": "sensor.garaje_humedad",
                        "state": "51",
                        "attributes": {"friendly_name": "Garaje humedad", "device_class": "humidity"},
                    },
                ]
            )
            result = await ha_search_entities(context(), {"query": "temperaturas salon", "domain": "sensor", "limit": 5})
            self.assertIn("sensor.salon_temperatura", result)
            self.assertNotIn("sensor.garaje_humedad", result)

        asyncio.run(run())

    def test_search_entities_treats_switch_as_domain_category(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.extend(
                [
                    {
                        "entity_id": "switch.unavailable_interruptor",
                        "state": "unavailable",
                        "attributes": {"friendly_name": "Interruptor antiguo"},
                    },
                    {
                        "entity_id": "switch.pia_comedor",
                        "state": "on",
                        "attributes": {"friendly_name": "piatele piatele rele"},
                    },
                    {
                        "entity_id": "switch.wall_panel_relay_1",
                        "state": "off",
                        "attributes": {"friendly_name": "Wall Panel Relay 1"},
                    },
                ]
            )
            result = await ha_search_entities(context(), {"query": "interruptor", "domain": "switch", "limit": 5})
            self.assertIn("switch.pia_comedor", result)
            self.assertIn("switch.wall_panel_relay_1", result)
            self.assertLess(result.index("switch.pia_comedor"), result.index("switch.unavailable_interruptor"))

        asyncio.run(run())

    def test_search_entities_keeps_location_when_switch_category_is_generic(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                write_storage(
                    root,
                    "core.area_registry",
                    "areas",
                    [
                        {"id": "salon", "name": "Salón", "aliases": ["Comedor"]},
                        {"id": "terreno", "name": "Terreno", "aliases": []},
                    ],
                )
                write_storage(
                    root,
                    "core.device_registry",
                    "devices",
                    [
                        {"id": "dev_salon", "name": "NSPanel", "area_id": "salon"},
                        {"id": "dev_terreno", "name": "Riego1", "area_id": "terreno"},
                    ],
                )
                write_storage(
                    root,
                    "core.entity_registry",
                    "entities",
                    [
                        {"entity_id": "switch.wall_panel_relay_1", "device_id": "dev_salon", "area_id": None},
                        {"entity_id": "switch.riego_rele1", "device_id": "dev_terreno", "area_id": None},
                    ],
                )
                FakeAsyncClient.states.extend(
                    [
                        {
                            "entity_id": "switch.riego_rele1",
                            "state": "off",
                            "attributes": {"friendly_name": "Riego1 riego_rele1"},
                        },
                        {
                            "entity_id": "switch.wall_panel_relay_1",
                            "state": "off",
                            "attributes": {"friendly_name": "Wall Panel Relay 1"},
                        },
                    ]
                )
                ctx = context()
                ctx.fs_roots = [root]
                result = await ha_search_entities(ctx, {"query": "interruptores salon comedor", "domain": "switch", "limit": 5})
                self.assertIn("switch.wall_panel_relay_1", result)
                self.assertNotIn("switch.riego_rele1", result)
                self.assertIn('"area_name":"Salón"', result)

        asyncio.run(run())

    def test_search_entities_uses_area_registry_for_inside_house(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                write_storage(
                    root,
                    "core.area_registry",
                    "areas",
                    [
                        {"id": "salon", "name": "Salón", "aliases": []},
                        {"id": "terreno", "name": "Terreno", "aliases": []},
                    ],
                )
                write_storage(
                    root,
                    "core.device_registry",
                    "devices",
                    [
                        {"id": "dev_salon", "name": "NSPanel", "area_id": "salon"},
                        {"id": "dev_terreno", "name": "G2", "area_id": "terreno"},
                    ],
                )
                write_storage(
                    root,
                    "core.entity_registry",
                    "entities",
                    [
                        {"entity_id": "sensor.wall_panel_temperature", "device_id": "dev_salon", "area_id": None},
                        {"entity_id": "sensor.temperatura_aire_g2", "device_id": "dev_terreno", "area_id": None},
                    ],
                )
                FakeAsyncClient.states.extend(
                    [
                        {
                            "entity_id": "sensor.temperatura_aire_g2",
                            "state": "36.5",
                            "attributes": {"friendly_name": "G2 Temperatura Aire g2", "device_class": "temperature"},
                        },
                        {
                            "entity_id": "sensor.wall_panel_temperature",
                            "state": "24.2",
                            "attributes": {"friendly_name": "Wall Panel Temperature", "device_class": "temperature"},
                        },
                    ]
                )
                ctx = context()
                ctx.fs_roots = [root]
                result = await ha_search_entities(ctx, {"query": "temperatura dentro de casa", "domain": "sensor", "limit": 5})
                salon_index = result.index("sensor.wall_panel_temperature")
                terreno_index = result.index("sensor.temperatura_aire_g2")
                self.assertLess(salon_index, terreno_index)
                self.assertIn('"area_name":"Salón"', result)

        asyncio.run(run())

    def test_get_state_resolves_friendly_name(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.append(
                {
                    "entity_id": "binary_sensor.puerta_cocina",
                    "state": "off",
                    "attributes": {"friendly_name": "Puerta cocina"},
                }
            )
            result = await ha_get_state(context(), {"entity_id": "Puerta Cocina", "domain": "binary_sensor"})
            self.assertIn('"resolved_entity_id":"binary_sensor.puerta_cocina"', result)
            self.assertIn('"state":"off"', result)

        asyncio.run(run())

    def test_call_service_resolves_target_friendly_name_conservatively(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.append(
                {
                    "entity_id": "light.patio",
                    "state": "off",
                    "attributes": {"friendly_name": "Patio"},
                }
            )
            result = await ha_call_service(
                context(require_action_confirmation=False),
                {
                    "domain": "light",
                    "service": "turn_on",
                    "target": {"entity_id": "patio"},
                    "confirm": True,
                },
            )
            self.assertIn('"entity_resolved:patio->light.patio"', result)
            self.assertEqual(FakeAsyncClient.posts[0]["json"]["entity_id"], "light.patio")
            self.assertNotIn("target", FakeAsyncClient.posts[0]["json"])

        asyncio.run(run())

    def test_call_service_rejects_ambiguous_friendly_name(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states.extend(
                [
                    {"entity_id": "light.patio_1", "state": "off", "attributes": {"friendly_name": "Patio"}},
                    {"entity_id": "light.patio_2", "state": "off", "attributes": {"friendly_name": "Patio"}},
                ]
            )
            with self.assertRaisesRegex(ValueError, "Entidad ambigua"):
                await ha_call_service(
                    context(require_action_confirmation=False),
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "target": {"entity_id": "patio"},
                        "confirm": True,
                    },
                )
            self.assertEqual(FakeAsyncClient.posts, [])

        asyncio.run(run())

    def test_mobile_alert_sets_volume_speaks_and_sends_critical_notification(self) -> None:
        async def run() -> None:
            result = await ha_send_mobile_alert(
                context(require_action_confirmation=True),
                {
                    "message": "Han llamado a la puerta",
                    "notify": "notify/mobile_app_sm_a566b",
                },
            )
            self.assertIn('"notify":"notify/mobile_app_sm_a566b"', result)
            self.assertEqual(len(FakeAsyncClient.posts), 3)
            self.assertEqual(
                FakeAsyncClient.posts[0]["json"],
                {
                    "message": "command_volume_level",
                    "data": {
                        "media_stream": "alarm_stream",
                        "command": 100,
                    },
                },
            )
            self.assertEqual(
                FakeAsyncClient.posts[1]["json"],
                {
                    "message": "TTS",
                    "data": {
                        "ttl": 0,
                        "priority": "high",
                        "media_stream": "alarm_stream",
                        "tts_text": "Han llamado a la puerta",
                    },
                },
            )
            self.assertEqual(FakeAsyncClient.posts[2]["json"]["message"], "Han llamado a la puerta")
            self.assertEqual(FakeAsyncClient.posts[2]["json"]["data"]["channel"], "alarm_stream")
            self.assertEqual(FakeAsyncClient.posts[2]["json"]["data"]["sound"], "default")
            self.assertEqual(
                FakeAsyncClient.posts[2]["json"]["data"]["push"]["sound"],
                {"name": "default", "critical": 1, "volume": 1.0},
            )
            self.assertEqual(
                FakeAsyncClient.posts[0]["url"],
                "http://homeassistant:8123/api/services/notify/mobile_app_sm_a566b",
            )

        asyncio.run(run())

    def test_mobile_alert_can_disable_speech_and_critical_mode(self) -> None:
        async def run() -> None:
            await ha_send_mobile_alert(
                context(require_action_confirmation=True),
                {
                    "message": "Aviso discreto",
                    "speak": False,
                    "critical": False,
                },
            )
            self.assertEqual(len(FakeAsyncClient.posts), 2)
            self.assertEqual(FakeAsyncClient.posts[1]["json"]["message"], "Aviso discreto")
            self.assertEqual(
                FakeAsyncClient.posts[1]["json"]["data"],
                {"ttl": 0, "priority": "normal"},
            )

        asyncio.run(run())

    def test_notify_call_service_does_not_require_extra_confirmation(self) -> None:
        async def run() -> None:
            await ha_call_service(
                context(require_action_confirmation=True),
                {
                    "domain": "notify",
                    "service": "mobile_app_sm_a566b",
                    "service_data": {
                        "message": "command_volume_level",
                        "data": {"media_stream": "alarm_stream", "command": 100},
                    },
                },
            )
            self.assertEqual(len(FakeAsyncClient.posts), 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
