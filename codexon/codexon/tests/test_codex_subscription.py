from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch

from services import codex_subscription as subscription


class SubscriptionTest(unittest.IsolatedAsyncioTestCase):
    def test_quota_retains_buckets_and_excludes_account_identity_and_credits(self):
        raw = {"accountId": "private", "rateLimitResetCredits": {"availableCount": 1},
               "rateLimitsByLimitId": {"codex": {"credits": {"balance": "20"}, "primary": {
                   "usedPercent": 68, "windowDurationMins": 300, "resetsAt": 1000}},
                   "other": {"limitName": "Otro", "secondary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 2000}}}}
        result = subscription.normalize_quota(raw)
        self.assertEqual(len(result["windows"]), 2)
        self.assertNotIn("accountId", result)
        self.assertNotIn("credits", str(result))
        self.assertEqual(result["windows"][0]["used_percent"], 68)
        self.assertEqual(subscription.normalize_quota({})["windows"], [])

    def test_delta_does_not_cross_resets_or_treat_decreases_as_savings(self):
        def quota(percent, reset=1000):
            return subscription.normalize_quota({"rateLimits": {"primary": {"usedPercent": percent, "windowDurationMins": 300, "resetsAt": reset}}})
        self.assertEqual(subscription.quota_changes(quota(68), quota(69))[0]["delta_percentage_points"], 1)
        self.assertEqual(subscription.quota_changes(quota(68), quota(68))[0]["delta_percentage_points"], 0)
        for end in (quota(1, 2000), quota(67), quota(68, None)):
            delta = subscription.quota_changes(quota(68), end)[0]
            self.assertIsNone(delta["delta_percentage_points"])
            self.assertTrue(delta["reset_or_changed"])
        self.assertEqual(subscription.quota_changes(None, quota(68)), [])

    async def test_catalog_pagination_and_image_capabilities(self):
        rpc = AsyncMock(side_effect=[
            {"data": [{"id": "text", "inputModalities": ["text"]}, {"id": "hidden", "hidden": True},
                      {"id": "vision", "inputModalities": ["text", "image"], "isDefault": True,
                       "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}], "nextCursor": "next"},
            {"data": [{"id": "older", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]},
            {"rateLimits": {"primary": {"usedPercent": 20}}},
        ])

        @asynccontextmanager
        async def session():
            yield rpc

        with patch.object(subscription, "_rpc_session", session):
            result = await subscription.read_subscription_state(include_models=True)
        self.assertEqual([m["id"] for m in result["models"]], ["vision", "older"])
        self.assertEqual(result["models"][0]["reasoning_effort"], "medium")
        self.assertEqual(result["default_model"], "vision")
        self.assertEqual(rpc.await_args_list[1].args[1]["cursor"], "next")

    async def test_unavailable_quota_does_not_hide_models(self):
        rpc = AsyncMock(side_effect=[{"data": [{"id": "vision"}]}, RuntimeError("unavailable")])

        @asynccontextmanager
        async def session():
            yield rpc

        with patch.object(subscription, "_rpc_session", session):
            result = await subscription.read_subscription_state(include_models=True)
        self.assertEqual(result["default_model"], "vision")
        self.assertIsNone(result["quota"])
        self.assertTrue(result["quota_error"])
