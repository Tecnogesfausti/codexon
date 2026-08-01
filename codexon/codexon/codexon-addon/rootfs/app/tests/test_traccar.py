from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest
from types import SimpleNamespace

from tools.traccar import requested_tracker, resolve_tracker, traccar_get_location


class FakeResponse:
    def __init__(self, payload=None, *, text: str = "", error: Exception | None = None) -> None:
        self._payload = payload
        self.text = text
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self):
        return self._payload


class FakeAsyncClient:
    devices: list[dict] = []
    positions: dict[int, dict] = {}
    addresses: dict[tuple[float, float], str] = {}

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, headers: dict, params: dict | None = None):
        if url.endswith("/api/devices"):
            return FakeResponse(self.devices)
        if url.endswith("/api/positions"):
            position = self.positions.get(int((params or {})["id"]))
            return FakeResponse([position] if position else [])
        if url.endswith("/api/server/geocode"):
            key = (float((params or {})["latitude"]), float((params or {})["longitude"]))
            return FakeResponse(text=self.addresses.get(key, ""))
        raise AssertionError(f"URL inesperada: {url}")


class FakeHttpx:
    AsyncClient = FakeAsyncClient


def context() -> SimpleNamespace:
    return SimpleNamespace(
        traccar_base_url="http://traccar:8082",
        traccar_api_token="secret",
        httpx=FakeHttpx,
    )


class TraccarToolTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsyncClient.devices = [
            {"id": 1, "name": "sinotrak", "status": "offline", "positionId": 11},
            {"id": 2, "name": "Samsung", "status": "online", "positionId": 22},
            {"id": 3, "name": "redmi", "status": "online", "positionId": 33},
        ]
        FakeAsyncClient.positions = {}
        FakeAsyncClient.addresses = {}

    def test_aliases_only_resolve_allowed_devices(self) -> None:
        self.assertEqual(resolve_tracker("mi moto")[0], "moto")
        self.assertEqual(resolve_tracker("teléfono Samsung")[0], "movil")
        self.assertEqual(requested_tracker("¿Dónde está mi moto?"), "moto")
        self.assertEqual(requested_tracker("Localiza mi móvil"), "movil")
        with self.assertRaisesRegex(ValueError, "moto.*movil"):
            resolve_tracker("redmi")

    def test_recent_motorbike_location_includes_address_status_and_map(self) -> None:
        async def run() -> None:
            fix_time = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)).isoformat()
            FakeAsyncClient.positions[11] = {
                "id": 11,
                "valid": True,
                "fixTime": fix_time,
                "latitude": 40.0,
                "longitude": -3.0,
                "speed": 0,
                "attributes": {"motion": False, "ignition": False},
            }
            FakeAsyncClient.addresses[(40.0, -3.0)] = "1 Calle de Ejemplo, Ciudad de ejemplo, ES"

            result = json.loads(await traccar_get_location(context(), {"device": "moto"}))

            self.assertTrue(result["is_current"])
            self.assertEqual(result["status"], "offline")
            self.assertIn("Calle de Ejemplo", result["recommended_answer"])
            self.assertIn("desconectado", result["recommended_answer"])
            self.assertIn("google.com/maps", result["recommended_answer"])

        asyncio.run(run())

    def test_stale_phone_location_never_claims_to_be_current(self) -> None:
        async def run() -> None:
            fix_time = (dt.datetime.now(dt.UTC) - dt.timedelta(days=10)).isoformat()
            FakeAsyncClient.positions[22] = {
                "id": 22,
                "valid": True,
                "fixTime": fix_time,
                "latitude": 40.1,
                "longitude": -3.1,
                "speed": 0,
                "attributes": {"motion": False},
            }
            FakeAsyncClient.addresses[(40.1, -3.1)] = "Ciudad de ejemplo, ES"

            result = json.loads(await traccar_get_location(context(), {"device": "movil"}))

            self.assertFalse(result["is_current"])
            self.assertEqual(result["freshness"], "antigua")
            self.assertIn("ultima posicion conocida", result["recommended_answer"].lower())
            self.assertIn("no puedo asegurar", result["recommended_answer"].lower())

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
