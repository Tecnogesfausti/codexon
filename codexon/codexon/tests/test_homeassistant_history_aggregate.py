from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools.homeassistant import (
    ha_aggregate_numeric_history,
    ha_get_long_term_statistics,
    ha_measure_numeric_during_state,
)


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.content = json.dumps(payload).encode()

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, headers: dict, params: dict | None = None):
        if url.endswith("/api/states"):
            return FakeResponse(
                [
                    {
                        "entity_id": "sensor.litros_diarios",
                        "state": "12",
                        "attributes": {"friendly_name": "Litros diarios", "unit_of_measurement": "L", "device_class": "water"},
                    }
                ]
            )
        if url.endswith("/api/states/sensor.litros_diarios"):
            return FakeResponse(
                {
                    "entity_id": "sensor.litros_diarios",
                    "state": "12",
                    "attributes": {"friendly_name": "Litros diarios", "unit_of_measurement": "L", "device_class": "water"},
                }
            )
        return FakeResponse(
            [
                [
                    {"entity_id": "sensor.litros_diarios", "state": "900", "last_changed": "2026-07-05T22:00:00+00:00"},
                    {"state": "0", "last_changed": "2026-07-05T22:00:00.002000+00:00"},
                    {"state": "100", "last_changed": "2026-07-06T10:00:00+00:00"},
                    {"state": "0", "last_changed": "2026-07-06T22:00:00+00:00"},
                    {"state": "250", "last_changed": "2026-07-07T20:00:00+00:00"},
                    {"state": "unavailable", "last_changed": "2026-07-07T21:00:00+00:00"},
                ]
            ]
        )


class FakeHttpx:
    AsyncClient = FakeAsyncClient


class NumericHistoryAggregateTest(unittest.TestCase):
    def test_daily_max_ignores_start_baseline_and_ranks_periods(self) -> None:
        async def run() -> None:
            context = SimpleNamespace(
                ha_base_url="http://homeassistant:8123",
                ha_token="token",
                httpx=FakeHttpx,
                memory=None,
                fs_roots=[],
            )
            raw = await ha_aggregate_numeric_history(
                context,
                {
                    "entity_id": "sensor.litros_diarios",
                    "start_time": "2026-07-06T00:00:00+02:00",
                    "end_time": "2026-07-08T00:00:00+02:00",
                    "timezone": "Europe/Madrid",
                    "group_by": "day",
                    "aggregation": "max",
                },
            )
            result = json.loads(raw)
            sensor = result["results"][0]

            self.assertEqual(sensor["unit"], "L")
            self.assertEqual(sensor["ignored_start_state"], 1)
            self.assertEqual(sensor["highest"]["period"], "2026-07-07")
            self.assertEqual(sensor["highest"]["weekday_name"], "martes")
            self.assertEqual(sensor["highest"]["value"], 250)
            self.assertEqual(sensor["periods"][0]["max"], 100)

        asyncio.run(run())

    def test_period_group_calculates_one_maximum_for_full_interval(self) -> None:
        async def run() -> None:
            context = SimpleNamespace(
                ha_base_url="http://homeassistant:8123",
                ha_token="token",
                httpx=FakeHttpx,
                memory=None,
                fs_roots=[],
            )
            raw = await ha_aggregate_numeric_history(
                context,
                {
                    "entity_id": "sensor.litros_diarios",
                    "start_time": "2026-07-06T00:00:00+02:00",
                    "end_time": "2026-07-08T00:00:00+02:00",
                    "timezone": "Europe/Madrid",
                    "group_by": "period",
                    "aggregation": "max",
                },
            )
            result = json.loads(raw)
            periods = result["results"][0]["periods"]

            self.assertEqual(len(periods), 1)
            self.assertEqual(periods[0]["period"], "period")
            self.assertEqual(periods[0]["value"], 250)
            self.assertEqual(periods[0]["median"], 50)
            self.assertIn("stddev_population", periods[0])
            self.assertIn("trend_per_day", periods[0])
            self.assertIn("last_zscore", periods[0])
            self.assertIn("last_anomaly", periods[0])

        asyncio.run(run())

    def test_python_selects_median_without_llm_arithmetic(self) -> None:
        async def run() -> None:
            context = SimpleNamespace(
                ha_base_url="http://homeassistant:8123",
                ha_token="token",
                httpx=FakeHttpx,
                memory=None,
                fs_roots=[],
            )
            raw = await ha_aggregate_numeric_history(
                context,
                {
                    "entity_id": "sensor.litros_diarios",
                    "start_time": "2026-07-06T00:00:00+02:00",
                    "end_time": "2026-07-08T00:00:00+02:00",
                    "group_by": "period",
                    "aggregation": "median",
                },
            )
            period = json.loads(raw)["results"][0]["periods"][0]

            self.assertEqual(period["value"], 50)
            self.assertEqual(period["value_semantics"], "median")

        asyncio.run(run())

    def test_attributes_numeric_consumption_only_while_activity_is_on(self) -> None:
        class StateGatedClient(FakeAsyncClient):
            async def get(self, url: str, headers: dict, params: dict | None = None):
                if url.endswith("/api/states"):
                    return FakeResponse(
                        [
                            {"entity_id": "switch.zone_bonsai", "state": "off", "attributes": {}},
                            {
                                "entity_id": "sensor.water_meter",
                                "state": "145",
                                "attributes": {"unit_of_measurement": "L", "device_class": "water"},
                            },
                        ]
                    )
                if url.endswith("/api/states/sensor.water_meter"):
                    return FakeResponse(
                        {
                            "entity_id": "sensor.water_meter",
                            "state": "145",
                            "attributes": {"unit_of_measurement": "L", "device_class": "water"},
                        }
                    )
                if params and params.get("filter_entity_id") == "switch.zone_bonsai":
                    return FakeResponse(
                        [[
                            {"entity_id": "switch.zone_bonsai", "state": "off", "last_changed": "2026-07-24T00:00:00+00:00"},
                            {"state": "on", "last_changed": "2026-07-24T10:00:00+00:00"},
                            {"state": "off", "last_changed": "2026-07-24T10:02:00+00:00"},
                            {"state": "on", "last_changed": "2026-07-25T10:00:00+00:00"},
                            {"state": "off", "last_changed": "2026-07-25T10:01:00+00:00"},
                        ]]
                    )
                if params and params.get("filter_entity_id") == "sensor.water_meter":
                    return FakeResponse(
                        [[
                            {"entity_id": "sensor.water_meter", "state": "100", "last_changed": "2026-07-24T00:00:00+00:00"},
                            {"state": "105", "last_changed": "2026-07-24T09:00:00+00:00"},
                            {"state": "115", "last_changed": "2026-07-24T10:01:00+00:00"},
                            {"state": "125", "last_changed": "2026-07-24T10:01:59+00:00"},
                            {"state": "130", "last_changed": "2026-07-24T12:00:00+00:00"},
                            {"state": "140", "last_changed": "2026-07-25T10:00:30+00:00"},
                            {"state": "145", "last_changed": "2026-07-25T10:00:59+00:00"},
                        ]]
                    )
                return await super().get(url, headers, params)

        class StateGatedHttpx:
            AsyncClient = StateGatedClient

        async def run() -> None:
            context = SimpleNamespace(
                ha_base_url="http://homeassistant:8123",
                ha_token="token",
                httpx=StateGatedHttpx,
                memory=None,
                fs_roots=[],
            )
            raw = await ha_measure_numeric_during_state(
                context,
                {
                    "activity_entity_id": "switch.zone_bonsai",
                    "measurement_entity_id": "sensor.water_meter",
                    "start_time": "2026-07-24T00:00:00+00:00",
                    "end_time": "2026-07-26T00:00:00+00:00",
                },
            )
            result = json.loads(raw)

            self.assertEqual(result["total"], 35)
            self.assertEqual(result["unit"], "L")
            self.assertEqual(result["active_intervals"], 2)
            self.assertEqual(result["intervals_with_data"], 2)
            self.assertTrue(result["coverage_complete"])
            self.assertEqual([row["value"] for row in result["intervals"]], [20, 15])

        asyncio.run(run())

    def test_long_term_statistics_sums_hourly_changes(self) -> None:
        async def run() -> None:
            context = SimpleNamespace(ha_base_url="http://homeassistant:8123", ha_token="token")
            websocket_result = {
                "sensor.energiap_casa": [
                    {"start": 1, "change": 0.2, "sum": 100.2},
                    {"start": 2, "change": 0.35, "sum": 100.55},
                ]
            }
            with patch(
                "tools.homeassistant.ha_websocket_command",
                new=AsyncMock(return_value=websocket_result),
            ):
                raw = await ha_get_long_term_statistics(
                    context,
                    {
                        "entity_id": "sensor.energiap_casa",
                        "start_time": "2026-06-10T00:00:00+02:00",
                        "end_time": "2026-06-11T00:00:00+02:00",
                    },
                )
            result = json.loads(raw)
            self.assertEqual(result["samples"], 2)
            self.assertEqual(result["value"], 0.55)
            self.assertEqual(result["calculation"], "sum(change)")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
