from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import httpx  # noqa: F401
except ImportError:
    sys.modules.setdefault("httpx", SimpleNamespace())

from codexon import (
    MemoryStore,
    ModelRequest,
    ModelRouter,
    CodexonAgent,
    extract_json_object,
    format_model_menu,
    format_model_search_results,
    print_model_catalog_pages,
    previous_calendar_week_range,
    resolve_model_reference,
)
from tools.homeassistant import (
    ha_get_state,
    ha_search_entities,
    ha_teach_entity_mapping,
    resolve_entity_reference,
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
    states: list[dict] = []

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

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


def context(memory: MemoryStore) -> SimpleNamespace:
    return SimpleNamespace(
        ha_base_url="http://homeassistant:8123",
        ha_token="token",
        httpx=FakeHttpx,
        memory=memory,
        fs_roots=[],
    )


class MemoryCatalogTest(unittest.TestCase):
    def test_interactive_model_setting_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.sqlite3"
            memory = MemoryStore(path)
            memory.set_setting("interactive_model", "openai/gpt-4.1")
            memory.close()

            reopened = MemoryStore(path)
            try:
                self.assertEqual(reopened.get_setting("interactive_model"), "openai/gpt-4.1")
                reopened.set_setting("interactive_model", None)
                self.assertEqual(reopened.get_setting("interactive_model"), "")
            finally:
                reopened.close()

    def test_model_menu_resolves_number_and_user_choice_bypasses_cost_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "models.yaml"
            config_path.write_text(
                """
default: cheap/model
fallbacks: [backup/model]
priorities:
  cost:
    max_input_price_per_million: 0.1
routes:
  homeassistant:
    model: cheap/model
  scheduled_task:
    model: smart/model
""".strip(),
                encoding="utf-8",
            )
            catalog = {
                "cheap/model": {"supports_tools": True, "input_price_per_million": 0.01, "output_price_per_million": 0.02, "input_price": 0.00000001, "output_price": 0.00000002},
                "smart/model": {"supports_tools": True, "input_price_per_million": 5.0, "output_price_per_million": 10.0, "input_price": 0.000005, "output_price": 0.00001},
                "backup/model": {"supports_tools": True, "input_price_per_million": 0.02, "output_price_per_million": 0.03, "input_price": 0.00000002, "output_price": 0.00000003},
                "other/model": {"supports_tools": True, "supports_chat": True, "input_price_per_million": 0.04, "output_price_per_million": 0.05, "input_price": 0.00000004, "output_price": 0.00000005},
                "no-tools/model": {"supports_tools": False, "supports_chat": True},
                "no-chat/model": {"supports_tools": True, "supports_chat": False},
            }
            router = ModelRouter(config_path, catalog)

            self.assertEqual(resolve_model_reference(router, "1"), "cheap/model")
            self.assertEqual(resolve_model_reference(router, "4"), "other/model")
            self.assertIn("smart/model", format_model_menu(router, "smart/model"))
            self.assertIn("Catalogo oficial OpenRouter", format_model_menu(router, None))
            self.assertIn("other/model", format_model_menu(router, None))
            self.assertIn("Mostrando 1-4 de 4 modelos", format_model_menu(router, None))
            self.assertIn("4. other/model", format_model_search_results(router, "other", ["other/model"]))
            self.assertNotIn("no-tools/model", format_model_menu(router, None))
            self.assertNotIn("no-chat/model", format_model_menu(router, None))
            self.assertEqual(router.search_models("no-tools"), [])
            selection = router.select(
                ModelRequest(
                    task="homeassistant",
                    prompt_tokens_estimate=100,
                    requires_tools=True,
                    preferred_model="smart/model",
                )
            )
            self.assertEqual(selection.model, "smart/model")

    def test_extract_json_object_accepts_markdown_fence(self) -> None:
        data = extract_json_object('texto previo ```json\n{"memories": []}\n``` texto final')
        self.assertEqual(data, {"memories": []})

    def test_memory_search_does_not_fall_back_to_unrelated_recent_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory.sqlite3")
            try:
                memory.add_memory(
                    kind="hecho",
                    topic="riego",
                    content="Los bonsais usan el programador secundario.",
                    confidence=0.9,
                    source="test",
                )

                self.assertEqual(memory.search_memories("temperatura dormitorio"), [])
                self.assertEqual(len(memory.search_memories("bonsais")), 1)
            finally:
                memory.close()

    def test_entity_catalog_keeps_significant_short_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory.sqlite3")
            try:
                memory.upsert_entity_catalog(
                    [
                        {
                            "entity_id": "sensor.energia_tv",
                            "friendly_name": "Energia TV",
                            "unit": "kWh",
                            "aliases": ["consumo total tv"],
                        },
                        {
                            "entity_id": "sensor.energia_casa",
                            "friendly_name": "Energia casa",
                            "unit": "kWh",
                            "aliases": ["consumo total casa"],
                        },
                    ],
                    source="test",
                )

                rows = memory.search_entity_catalog("consumo total TV", domain="sensor", limit=2)

                self.assertEqual(rows[0]["entity_id"], "sensor.energia_tv")
            finally:
                memory.close()

    def test_entity_catalog_preserves_learned_aliases_on_live_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "memory.sqlite3")
            try:
                memory.upsert_entity_catalog(
                    [{"entity_id": "sensor.litros_diarios", "aliases": ["consumo diario agua"]}],
                    source="catalog",
                )
                memory.upsert_entity_catalog(
                    [{"entity_id": "sensor.litros_diarios", "state": "10", "aliases": []}],
                    source="live",
                )

                row = memory.search_entity_catalog("consumo diario agua", domain="sensor", limit=1)[0]

                self.assertEqual(row["entity_id"], "sensor.litros_diarios")
                self.assertIn("consumo diario agua", json.loads(row["aliases"]))
            finally:
                memory.close()

    def test_previous_calendar_week_uses_monday_boundaries(self) -> None:
        now = __import__("datetime").datetime.fromisoformat("2026-07-16T12:00:00+02:00")

        start, end = previous_calendar_week_range(now)

        self.assertEqual(start.isoformat(), "2026-07-06T00:00:00+02:00")
        self.assertEqual(end.isoformat(), "2026-07-13T00:00:00+02:00")

    def test_homeassistant_search_persists_catalog_and_resolution(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states = [
                {
                    "entity_id": "switch.nspanel_relay_1",
                    "state": "off",
                    "attributes": {"friendly_name": "NSPanel Relay 1"},
                    "last_changed": "2026-07-14T09:00:00+00:00",
                    "last_updated": "2026-07-14T09:00:00+00:00",
                },
                {
                    "entity_id": "light.nspanel_backlight",
                    "state": "on",
                    "attributes": {"friendly_name": "NSPanel Backlight"},
                    "last_changed": "2026-07-14T09:00:00+00:00",
                    "last_updated": "2026-07-14T09:00:00+00:00",
                },
            ]
            with tempfile.TemporaryDirectory() as tmp:
                memory = MemoryStore(Path(tmp) / "memory.sqlite3")
                try:
                    ctx = context(memory)
                    await ha_search_entities(ctx, {"query": "nspanel relay", "domain": "switch", "limit": 5})
                    await ha_get_state(ctx, {"query": "nspanel relay 1", "domain": "switch"})

                    catalog = memory.search_entity_catalog("nspanel relay", domain="switch", limit=5)
                    resolutions = memory.recent_entity_resolutions("nspanel relay", domain="switch", limit=5)
                    self.assertEqual(catalog[0]["entity_id"], "switch.nspanel_relay_1")
                    self.assertEqual(resolutions[0]["resolved_entity_id"], "switch.nspanel_relay_1")
                finally:
                    memory.close()

        asyncio.run(run())

    def test_entity_teachings_override_aliases_and_survive_restarts(self) -> None:
        async def run() -> None:
            FakeAsyncClient.states = [
                {
                    "entity_id": "switch.riego2_rele3",
                    "state": "off",
                    "attributes": {"friendly_name": "Riego2 relé 3"},
                },
                {
                    "entity_id": "switch.controlh2oficina_relealmacen4",
                    "state": "off",
                    "attributes": {"friendly_name": "Válvula general"},
                },
                {
                    "entity_id": "sensor.temperatura_nuevo",
                    "state": "22",
                    "attributes": {"friendly_name": "Temperatura nueva"},
                },
            ]
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "memory.sqlite3"
                memory = MemoryStore(path)
                ctx = context(memory)
                await ha_teach_entity_mapping(
                    ctx,
                    {
                        "operation": "alias",
                        "alias": "grifo del estanque",
                        "target_entity_id": "switch.riego2_rele3",
                        "notes": "Corrección del usuario",
                    },
                )
                await ha_teach_entity_mapping(
                    ctx,
                    {
                        "operation": "replace",
                        "old_entity_id": "sensor.temperatura_viejo",
                        "target_entity_id": "sensor.temperatura_nuevo",
                    },
                )

                alias_target, alias_warnings = await resolve_entity_reference(
                    ctx, query="grifo del estanque", domain="switch"
                )
                replacement_target, replacement_warnings = (
                    await resolve_entity_reference(
                        ctx,
                        entity_id="sensor.temperatura_viejo",
                        domain="sensor",
                    )
                )
                self.assertEqual(alias_target, "switch.riego2_rele3")
                self.assertTrue(any("entity_taught:" in item for item in alias_warnings))
                self.assertEqual(replacement_target, "sensor.temperatura_nuevo")
                self.assertTrue(
                    any("entity_taught:" in item for item in replacement_warnings)
                )
                memory.close()

                reopened = MemoryStore(path)
                try:
                    self.assertEqual(
                        reopened.resolve_taught_entity("abre el grifo del estanque"),
                        "switch.riego2_rele3",
                    )
                    self.assertEqual(
                        reopened.resolve_taught_entity("sensor.temperatura_viejo"),
                        "sensor.temperatura_nuevo",
                    )
                    self.assertTrue(
                        reopened.remove_entity_teaching(
                            teaching_type="alias",
                            key_text="grifo del estanque",
                        )
                    )
                    self.assertIsNone(
                        reopened.resolve_taught_entity("grifo del estanque")
                    )
                finally:
                    reopened.close()

        asyncio.run(run())


class ModelCatalogPaginationTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_shows_fifty_models_then_asks_for_next_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "models.yaml"
            config_path.write_text("default: test/model-000\nroutes: {}\n", encoding="utf-8")
            catalog = {
                f"test/model-{index:03d}": {
                    "supports_tools": True,
                    "context_length": 1000,
                    "input_price_per_million": 0,
                    "output_price_per_million": 0,
                }
                for index in range(51)
            }
            router = ModelRouter(config_path, catalog)
            with patch("codexon.async_input", new=AsyncMock(return_value="s")) as next_prompt:
                with patch("builtins.print") as output:
                    await print_model_catalog_pages(router, None)

            rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
            self.assertIn("pagina 1/2", rendered)
            self.assertIn("Mostrando 1-50 de 51 modelos", rendered)
            self.assertIn("pagina 2/2", rendered)
            self.assertIn("51. test/model-050", rendered)
            next_prompt.assert_awaited_once_with("\n¿Siguiente? [s/N] ")


class ModelRoutingRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_selected_interactive_model_applies_to_scheduled_task_chat(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        agent.request_preferred_model = "openrouter/free"
        agent.openai_tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]

        class FakeRouter:
            config = {"routes": {}}

            def __init__(self) -> None:
                self.preferred_model = None

            def select(self, request: ModelRequest):
                self.preferred_model = request.preferred_model
                return SimpleNamespace(
                    model=request.preferred_model or "paid/model",
                    reason="test",
                    estimated_cost_usd=0.0,
                    fallbacks=[],
                )

            def price_tuple(self, model: str):
                return (0.0, 0.0)

        class FakeCompletions:
            async def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                    model_extra={"provider": "test"},
                )

        class FakeMemory:
            def get_setting(self, key: str, default: str = "") -> str:
                return default

            def add_usage_event(self, **kwargs) -> None:
                self.usage = kwargs

            def add_event(self, **kwargs) -> None:
                raise AssertionError(kwargs)

        router = FakeRouter()
        agent.router = router
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        agent.memory = FakeMemory()

        await agent._chat([{"role": "user", "content": "haz tarea"}], task="scheduled_task")

        self.assertEqual(router.preferred_model, "openrouter/free")
        self.assertEqual(agent.memory.usage["model"], "openrouter/free")

    async def test_each_reasoning_profile_has_an_independent_model(self) -> None:
        agent = CodexonAgent.__new__(CodexonAgent)
        agent.request_preferred_model = "general/model"
        agent.openai_tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]

        class FakeRouter:
            config = {"routes": {}}

            def __init__(self) -> None:
                self.requests = []

            def select(self, request: ModelRequest):
                self.requests.append(request)
                return SimpleNamespace(
                    model=request.preferred_model or "automatic/model",
                    reason="test",
                    estimated_cost_usd=0.0,
                    fallbacks=[],
                )

            def price_tuple(self, model: str):
                return (0.0, 0.0)

        class FakeCompletions:
            async def create(self, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                    model_extra={"provider": "test"},
                )

        class FakeMemory:
            settings = {
                "interactive_model": "general/model",
                "classification_model": "classifier/model",
                "statistical_planning_model": "planner/model",
                "statistical_reasoning_model": "reasoner/model",
            }

            def get_setting(self, key: str, default: str = "") -> str:
                return self.settings.get(key, default)

            def add_usage_event(self, **kwargs) -> None:
                pass

            def add_event(self, **kwargs) -> None:
                raise AssertionError(kwargs)

        router = FakeRouter()
        agent.router = router
        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        agent.memory = FakeMemory()

        for task in (
            "homeassistant",
            "classification",
            "statistical_planning",
            "statistical_reasoning",
        ):
            await agent._chat([{"role": "user", "content": "prueba"}], task=task)

        agent.request_budget_mode = "economy"
        await agent._chat(
            [{"role": "user", "content": "prueba economica"}],
            task="homeassistant",
        )

        self.assertEqual(
            [request.preferred_model for request in router.requests],
            [
                "general/model",
                "classifier/model",
                "planner/model",
                "reasoner/model",
                "google/gemini-2.5-flash-lite",
            ],
        )


if __name__ == "__main__":
    unittest.main()
