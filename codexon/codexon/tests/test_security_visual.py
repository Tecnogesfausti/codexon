import asyncio
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codexon import MemoryStore, CodexonAgent
from event_engine.engine import EventEngine
from services.security_visual import (
    CameraConfig, SecurityVisualSupervisor, VisualVerdict, camera_configs, FALLBACK_MODEL, MODEL_NOTICE_ID,
    inconclusive, recent_cases, set_case_label, timestamp, validate_verdict,
)
from site_profile import SiteProfile


def event(entity="binary_sensor.casa_person", value="on", old="off", **attrs):
    return {"event_type": "state_changed", "time_fired": dt.datetime.now(dt.UTC).isoformat(), "data": {"entity_id": entity, "old_state": {"state": old}, "new_state": {"state": value, "attributes": attrs}}}


def frigate_event(**overrides):
    now = time.time()
    after = {"id": "event-1", "label": "person", "camera": "casa", "start_time": now, "snapshot": {"frame_time": now}, "has_snapshot": True}
    after.update(overrides)
    return event("sensor.frigate_event", "event-1", after=after)


def verdict(case_id="1", **overrides):
    value = {"candidate_id": case_id, "visual_verdict": "person_visible", "person_count": 1, "image_quality": "adequate", "time_alignment": "matched", "observations": ["Persona visible junto a la puerta."], "alternative_explanation": None, "limitations": [], "needs_more_evidence": False}
    value.update(overrides)
    return value


class VisualValidationTests(unittest.TestCase):
    def test_empty_current_image_cannot_filter_a_past_detection(self):
        raw = verdict(visual_verdict="supported_false_positive", person_count=0, alternative_explanation="Reflejo visible")
        checked = validate_verdict(json.dumps(raw), "1", [{"alignment": "unknown"}])
        self.assertEqual(checked["visual_verdict"], "inconclusive")
        self.assertEqual(checked["time_alignment"], "unknown")

    def test_false_positive_requires_matched_frame_and_explanation(self):
        raw = verdict(visual_verdict="supported_false_positive", person_count=0, alternative_explanation="Sombra de una rama en la caja señalada.")
        self.assertEqual(validate_verdict(json.dumps(raw), "1", [{"alignment": "matched"}])["visual_verdict"], "supported_false_positive")
        raw["alternative_explanation"] = None
        self.assertEqual(validate_verdict(json.dumps(raw), "1", [{"alignment": "matched"}])["visual_verdict"], "inconclusive")

    def test_missing_poor_or_contradictory_frames_are_inconclusive(self):
        for frames, kwargs in [([], {}), ([{"alignment": "matched"}], {"image_quality": "poor"}), ([{"alignment": "matched"}], {"person_count": 0})]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(validate_verdict(json.dumps(verdict(**kwargs)), "1", frames)["visual_verdict"], "inconclusive")

    def test_wrong_id_actions_unknown_keys_and_string_numbers_are_rejected(self):
        for kwargs in [{"candidate_id": "999"}, {"action": "disable_alarm"}, {"person_count": "1"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                validate_verdict(json.dumps(verdict(**kwargs)), "1", [{"alignment": "matched"}])

    def test_pilot_configuration_cannot_enable_filtering(self):
        profile = SiteProfile(path=None, roles={"security.visual.casa": {"enabled": True, "mode": "active", "entity_id": "camera.casa", "presence_entities": ["binary_sensor.casa_person"]}})
        with self.assertRaises(ValueError):
            camera_configs(profile)

    def test_non_finite_timestamps_are_not_evidence(self):
        self.assertIsNone(timestamp(float("nan")))
        self.assertIsNone(timestamp(float("inf")))


class VisualWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.directory.name) / "memory.sqlite3")
        self.memory.set_setting("image_analysis_model", "visual/test")
        self.config = CameraConfig("casa", "camera.casa", ("binary_sensor.casa_person",), ("casa",), daily_call_limit=2)
        self.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(verdict())))], usage=None))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        client.with_options = Mock(return_value=client)
        # No tools, messenger or physical executor are supplied to the observer.
        self.agent = SimpleNamespace(memory=self.memory, site_profile=SiteProfile.empty(), client=client, record_usage=Mock())
        self.worker = SecurityVisualSupervisor(self.agent, configs=(self.config,), log=Mock())
        self.frames = [{"alignment": "matched", "bytes": b"image", "media_type": "image/jpeg", "path": str(Path(self.directory.name) / "frame.jpg"), "sha256": "hash", "source": "frigate_event", "frame_time": time.time(), "received_at": dt.datetime.now(dt.UTC).isoformat()}]
        self.worker.capture = AsyncMock(return_value=self.frames)

    def tearDown(self):
        self.memory.conn.close()
        self.directory.cleanup()

    async def test_review_has_no_actions_and_registers_usage(self):
        case_id = self.worker.offer(frigate_event())
        await self.worker.process_one()
        row = recent_cases(self.memory.conn)[0]
        self.assertEqual(row["id"], case_id)
        self.assertEqual(row["result"]["visual_verdict"], "person_visible")
        self.agent.record_usage.assert_called_once()
        request = self.create.call_args.kwargs
        self.assertNotIn("tools", request)
        self.assertEqual(request["max_tokens"], 1000)
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertNotIn("path", row["reviews"][0]["frames"][0])
        self.assertTrue(row["reviews"][0]["frames"][0]["url"].startswith("api/"))

    async def test_incompatible_general_model_uses_security_fallback_without_changing_preference(self):
        self.memory.set_setting("image_analysis_model", "amazon/nova-lite-v1")
        self.agent.router = SimpleNamespace(model_catalog={"amazon/nova-lite-v1": {"supports_images": True, "supports_structured_outputs": False}})
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.assertEqual(self.create.call_args.kwargs["model"], "google/gemini-2.5-flash-lite")
        self.assertEqual(self.memory.get_setting("image_analysis_model"), "amazon/nova-lite-v1")
        self.assertEqual(recent_cases(self.memory.conn)[0]["result"]["visual_verdict"], "person_visible")
        selection=json.loads(self.memory.get_setting("security_visual.model_selection"))
        self.assertEqual(selection["effective_model"], "google/gemini-2.5-flash-lite")
        self.assertTrue(selection["fallback_reason"])

    async def test_compatible_general_model_is_used_and_fallback_status_clears(self):
        self.memory.set_setting("image_analysis_model", "compatible/vision")
        self.agent.router = SimpleNamespace(model_catalog={"compatible/vision": {"supports_images": True, "supports_structured_outputs": True}})
        self.memory.set_setting("security_visual.model_selection", json.dumps({"fallback_reason":"old selection"}))
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.assertEqual(self.create.call_args.kwargs["model"], "compatible/vision")
        self.assertIsNone(json.loads(self.memory.get_setting("security_visual.model_selection"))["fallback_reason"])

    async def test_qwen_gets_explicit_json_prompt_without_reasoning_and_keeps_strict_validation(self):
        self.memory.set_setting("image_analysis_model", "qwen/qwen3.5-flash-02-23")
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        request=self.create.call_args.kwargs
        self.assertFalse(request["extra_body"]["reasoning"]["enabled"])
        self.assertIn("JSON", request["messages"][0]["content"])
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        self.assertNotIn("tools", request)

    async def test_model_failure_recovers_with_gemini_same_frames_and_counts_both_attempts(self):
        response=self.create.return_value
        self.create.side_effect=[RuntimeError("private provider URL"),response]
        self.worker._model_catalog=AsyncMock(return_value={"visual/test",FALLBACK_MODEL})
        self.worker._create_model_notice=AsyncMock()
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.assertEqual(recent_cases(self.memory.conn)[0]["result"]["visual_verdict"],"person_visible")
        requests=[c.kwargs for c in self.create.call_args_list]
        self.assertEqual([r['model'] for r in requests],["visual/test",FALLBACK_MODEL])
        self.assertEqual(requests[0]['messages'][1],requests[1]['messages'][1])
        self.worker.capture.assert_awaited_once()
        self.assertEqual(self.memory.conn.execute('SELECT count(*) FROM security_visual_reviews WHERE llm_called=1').fetchone()[0],2)
        self.assertEqual(self.worker.selected_model(),FALLBACK_MODEL)
        restarted=SecurityVisualSupervisor(self.agent,configs=(self.config,),log=Mock())
        self.assertEqual(restarted.selected_model(),FALLBACK_MODEL)
        self.worker._create_model_notice.assert_not_awaited()
        self.assertNotIn('private',json.dumps(recent_cases(self.memory.conn)))
        self.assertEqual(json.loads(self.memory.get_setting('security_visual.ai_status'))['state'],'fallback')

    async def test_fallback_respects_daily_quota(self):
        self.worker.configs=(CameraConfig("casa","camera.casa",("binary_sensor.casa_person",),("casa",),daily_call_limit=1),)
        self.create.side_effect=RuntimeError("provider down")
        self.worker._model_catalog=AsyncMock(return_value={"visual/test",FALLBACK_MODEL})
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.create.assert_awaited_once()
        self.assertEqual(recent_cases(self.memory.conn)[0]['result']['visual_verdict'],'inconclusive')
        self.assertEqual(self.memory.conn.execute('SELECT count(*) FROM security_visual_reviews WHERE llm_called=1').fetchone()[0],1)

    async def test_fallback_false_positive_needs_second_opinion_and_cannot_bypass_quota(self):
        self.worker.configs=(CameraConfig("casa","camera.casa",("binary_sensor.casa_person",),("casa",),daily_call_limit=2,review_model='strong/review'),)
        raw=verdict(visual_verdict='supported_false_positive',person_count=0,alternative_explanation='Sombra de una rama visible.')
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw)))],usage=None)
        self.create.side_effect=[RuntimeError('primary unavailable'),response]
        self.worker._model_catalog=AsyncMock(return_value={'visual/test',FALLBACK_MODEL})
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.assertEqual(self.create.await_count,2)
        self.assertEqual(recent_cases(self.memory.conn)[0]['result']['visual_verdict'],'inconclusive')

    async def test_both_models_down_remain_inconclusive_without_retry_loop(self):
        self.create.side_effect=RuntimeError("both down")
        self.worker._model_catalog=AsyncMock(return_value={"visual/test",FALLBACK_MODEL})
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.assertEqual(self.create.await_count,2)
        self.assertEqual(recent_cases(self.memory.conn)[0]['result']['visual_verdict'],'inconclusive')
        self.assertEqual(json.loads(self.memory.get_setting('security_visual.ai_status'))['state'],'error')

    async def test_confirmed_removed_model_switches_before_llm_and_sends_one_ha_notice(self):
        self.worker._model_catalog=AsyncMock(return_value={FALLBACK_MODEL})
        self.worker._create_model_notice=AsyncMock()
        await self.worker.model_watchdog(force=True)
        await self.worker.model_watchdog(force=True)
        self.assertEqual(self.worker.selected_model(),FALLBACK_MODEL)
        self.create.assert_not_awaited()
        self.worker._create_model_notice.assert_awaited_once()
        payload=self.worker._create_model_notice.call_args.args[0]
        self.assertEqual(payload['notification_id'],MODEL_NOTICE_ID)
        self.assertIn('visual/test',payload['message'])
        self.assertIn(FALLBACK_MODEL,payload['message'])
        self.assertTrue(json.loads(self.memory.get_setting('security_visual.model_removal_notice'))['sent'])
        restarted=SecurityVisualSupervisor(self.agent,configs=(self.config,),log=Mock())
        with patch('services.security_visual.time.time',return_value=time.time()+86400):
            self.assertEqual(restarted.selected_model(),FALLBACK_MODEL)

    async def test_failed_catalog_does_not_claim_model_removed_or_send_notice(self):
        self.worker._model_catalog=AsyncMock(side_effect=RuntimeError('private network URL'))
        self.worker._create_model_notice=AsyncMock()
        await self.worker.model_watchdog(force=True)
        self.assertEqual(self.worker.selected_model(),'visual/test')
        self.worker._create_model_notice.assert_not_awaited()
        self.assertEqual(self.memory.get_setting('security_visual.model_removal_notice',''),'')
        self.assertNotIn('private',self.memory.get_setting('security_visual.model_catalog_check'))

    async def test_empty_catalog_is_not_evidence_of_a_removed_model(self):
        response=SimpleNamespace(raise_for_status=Mock(),json=Mock(return_value={'data':[]}))
        http=SimpleNamespace(get=AsyncMock(return_value=response))
        session=AsyncMock();session.__aenter__.return_value=http
        self.agent.httpx=SimpleNamespace(AsyncClient=Mock(return_value=session))
        self.worker._create_model_notice=AsyncMock()
        await self.worker.model_watchdog(force=True)
        self.worker._create_model_notice.assert_not_awaited()
        self.assertEqual(self.worker.selected_model(),'visual/test')
        self.assertIsNone(json.loads(self.memory.get_setting('security_visual.model_catalog_check'))['available'])

    async def test_ha_notice_calls_only_persistent_notification_service(self):
        response=SimpleNamespace(raise_for_status=Mock())
        http=SimpleNamespace(post=AsyncMock(return_value=response))
        session=AsyncMock();session.__aenter__.return_value=http
        self.agent.httpx=SimpleNamespace(AsyncClient=Mock(return_value=session))
        self.agent.ha_base_url='http://ha.example.test'
        self.agent.ha_token='test-token'
        payload={'notification_id':MODEL_NOTICE_ID,'title':'test','message':'test'}
        await self.worker._create_model_notice(payload)
        self.assertEqual(http.post.call_args.args[0],'http://ha.example.test/api/services/persistent_notification/create')
        self.assertEqual(http.post.call_args.kwargs['json'],payload)
        self.assertFalse(self.agent.httpx.AsyncClient.call_args.kwargs['follow_redirects'])

    async def test_failed_ha_notice_is_retried_later_without_spamming(self):
        self.worker._model_catalog=AsyncMock(return_value={FALLBACK_MODEL})
        self.worker._create_model_notice=AsyncMock(side_effect=[RuntimeError('HA temporarily offline'),None])
        await self.worker.model_watchdog(force=True)
        self.assertFalse(json.loads(self.memory.get_setting('security_visual.model_removal_notice'))['sent'])
        await self.worker.model_watchdog(force=True)
        self.assertEqual(self.worker._create_model_notice.await_count,1)
        with patch('services.security_visual.time.time',return_value=time.time()+301):
            await self.worker.send_model_removal_notice()
        self.assertEqual(self.worker._create_model_notice.await_count,2)
        self.assertTrue(json.loads(self.memory.get_setting('security_visual.model_removal_notice'))['sent'])

    async def test_catalog_recovery_restores_model_and_allows_new_removal_notice(self):
        self.worker._model_catalog=AsyncMock(side_effect=[{FALLBACK_MODEL},{FALLBACK_MODEL,'visual/test'},{FALLBACK_MODEL}])
        self.worker._create_model_notice=AsyncMock()
        await self.worker.model_watchdog(force=True)
        await self.worker.model_watchdog(force=True)
        self.assertEqual(self.worker.selected_model(),'visual/test')
        self.assertTrue(json.loads(self.memory.get_setting('security_visual.model_removal_notice'))['resolved'])
        await self.worker.model_watchdog(force=True)
        self.assertEqual(self.worker._create_model_notice.await_count,2)

    async def test_pending_removal_notice_is_cancelled_after_user_changes_model(self):
        self.worker._model_catalog=AsyncMock(return_value={FALLBACK_MODEL})
        self.worker._create_model_notice=AsyncMock(side_effect=RuntimeError('HA unavailable'))
        await self.worker.model_watchdog(force=True)
        self.memory.set_setting('image_analysis_model','new/model')
        with patch('services.security_visual.time.time',return_value=time.time()+301):
            await self.worker.send_model_removal_notice()
        self.assertEqual(self.worker._create_model_notice.await_count,1)
        self.assertTrue(json.loads(self.memory.get_setting('security_visual.model_removal_notice'))['resolved'])

    async def test_tapo_and_frigate_are_one_episode(self):
        first = self.worker.offer(event())
        second = self.worker.offer(frigate_event())
        self.assertEqual(first, second)
        row = recent_cases(self.memory.conn)[0]
        self.assertEqual(row["candidate"]["sources"], ["frigate", "tapo"])
        await self.worker.process_one()
        self.create.assert_awaited_once()

    async def test_duplicate_event_and_unchanged_frame_do_not_repeat_model(self):
        ev = frigate_event()
        first = self.worker.offer(ev)
        await self.worker.process_one()
        self.assertEqual(self.worker.offer(ev), first)
        self.assertFalse(await self.worker.process_one())
        self.create.assert_awaited_once()

    async def test_improved_snapshot_gets_only_one_additional_review(self):
        ev = frigate_event()
        case_id = self.worker.offer(ev)
        await self.worker.process_one()
        for offset in (1, 2, 3):
            ev["data"]["new_state"]["attributes"]["after"]["snapshot"]["frame_time"] += offset
            self.assertEqual(self.worker.offer(ev), case_id)
            await self.worker.process_one()
        self.assertEqual(self.create.await_count, 2)

    async def test_budget_stops_new_llm_calls(self):
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        ev = frigate_event(id="event-2", start_time=time.time() - 10)
        self.worker.offer(ev)
        await self.worker.process_one()
        self.assertEqual(self.create.await_count, 1)  # cooldown also protects the daily budget
        self.assertEqual(recent_cases(self.memory.conn)[0]["result"]["visual_verdict"], "inconclusive")

    async def test_daily_budget_applies_after_cooldown_too(self):
        self.worker.configs = (CameraConfig("casa", "camera.casa", ("binary_sensor.casa_person",), ("casa",), daily_call_limit=1),)
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        old = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2)).isoformat()
        self.memory.conn.execute("UPDATE security_visual_reviews SET started_at=?", (old,))
        self.memory.conn.commit()
        self.worker.offer(frigate_event(id="second", start_time=time.time() - 10))
        await self.worker.process_one()
        self.assertEqual(self.create.await_count, 1)

    async def test_pending_survives_restart_and_queue_overflow_is_inconclusive(self):
        case_id = self.worker.offer(frigate_event())
        for index in range(20):
            self.worker.offer(frigate_event(id=f"queued-{index}", start_time=time.time() - (index + 1) * 10))
        rows = recent_cases(self.memory.conn)
        self.assertEqual(sum(row["status"] == "pending" for row in rows), 16)
        self.assertTrue(any(row["result"] and row["result"]["visual_verdict"] == "inconclusive" for row in rows))
        restarted = SecurityVisualSupervisor(self.agent, configs=(self.config,), log=Mock())
        restarted.capture = self.worker.capture
        await restarted.process_one()
        row = self.memory.conn.execute("SELECT status FROM security_visual_cases WHERE id=?", (case_id,)).fetchone()
        self.assertEqual(row["status"], "reviewed")

    async def test_capture_failure_model_failure_and_bad_json_remain_inconclusive(self):
        for index, failure in enumerate(("capture", "model", "json")):
            with self.subTest(failure=failure):
                self.memory.conn.execute("DELETE FROM security_visual_reviews")
                self.memory.conn.commit()
                self.worker.offer(frigate_event(id=f"event-{index}", start_time=time.time() - (index + 1) * 10))
                self.worker.capture.side_effect = RuntimeError("private URL") if failure == "capture" else None
                self.create.side_effect = RuntimeError("private token") if failure == "model" else None
                self.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="invalid JSON"))])
                await self.worker.process_one()
                row = recent_cases(self.memory.conn)[0]
                self.assertEqual(row["result"]["visual_verdict"], "inconclusive")
                self.assertNotIn("private", json.dumps(row))

    async def test_tardy_tapo_candidate_does_not_capture_current_scene(self):
        ev = event()
        ev["time_fired"] = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2)).isoformat()
        self.worker.offer(ev)
        # Capture must reject a stale detection even if the bus delivered it now.
        with patch.object(SecurityVisualSupervisor, "_get_image", new_callable=AsyncMock) as get_image:
            frames = await SecurityVisualSupervisor.capture(self.worker, 1, recent_cases(self.memory.conn)[0]["candidate"], self.config)
        self.assertEqual(frames, [])
        get_image.assert_not_awaited()

    async def test_other_camera_label_and_off_transitions_are_ignored(self):
        for ev in [frigate_event(camera="otra"), frigate_event(label="cat"), event(value="off", old="on"), event(value="on", old="on")]:
            self.assertIsNone(self.worker.offer(ev))
        self.assertEqual(recent_cases(self.memory.conn), [])

    async def test_pending_survives_restart_but_started_review_is_not_replayed(self):
        case_id = self.worker.offer(frigate_event())
        self.memory.conn.execute("UPDATE security_visual_cases SET status='reviewing' WHERE id=?", (case_id,))
        self.memory.conn.commit()
        restarted = SecurityVisualSupervisor(self.agent, configs=(self.config,), log=Mock())
        self.assertFalse(await restarted.process_one())
        self.assertEqual(recent_cases(self.memory.conn)[0]["result"]["visual_verdict"], "inconclusive")
        self.create.assert_not_awaited()

    async def test_label_persists_without_changing_visual_result(self):
        case_id = self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.assertTrue(set_case_label(self.memory.conn, case_id, "false_detection"))
        self.assertEqual(recent_cases(self.memory.conn)[0]["label"], "false_detection")
        self.assertEqual(recent_cases(self.memory.conn)[0]["result"]["visual_verdict"], "person_visible")
        with self.assertRaises(ValueError):
            set_case_label(self.memory.conn, case_id, "turn_off")

    async def test_event_reader_only_persists_candidates(self):
        self.agent.security_visual_supervisor = self.worker
        engine = EventEngine(self.agent, runtime_log=Mock())
        self.assertEqual(await engine.dispatch(event()), 0)
        self.assertEqual(len(recent_cases(self.memory.conn)), 1)
        self.create.assert_not_awaited()
        self.worker.capture.assert_not_awaited()


class WaterSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopped_valve_unavailable_meter_and_counter_reset_never_reopen(self):
        for meter_value, switch_state in [("102", "off"), ("unavailable", "on"), ("90", "on"), ("nan", "on")]:
            with self.subTest(meter=meter_value, switch=switch_state):
                agent = CodexonAgent.__new__(CodexonAgent)
                agent.current_task_id = None
                async def read(name, args):
                    return json.dumps({"state": meter_value if args["entity_id"] == "sensor.meter" else switch_state})
                agent.call_builtin_tool = read
                agent.set_water_switches_state = AsyncMock()
                agent.restore_appdaemon_manual_water_target = AsyncMock()
                agent.send_water_task_alert = AsyncMock()
                agent.reschedule_current_task = AsyncMock()
                payload = {"switch_entity_id": "switch.custom_water", "master_switch_entity_id": "switch.master", "meter_entity_id": "sensor.meter", "target_liters": 10, "baseline_liters": 100, "started_at": dt.datetime.now(dt.UTC).isoformat(), "required_valve_states": {}, "alarm_entity_ids": []}
                with self.assertRaises(RuntimeError):
                    await agent.try_execute_water_liters_task("DETERMINISTIC_WATER_LITERS " + json.dumps(payload))
                self.assertTrue(agent.set_water_switches_state.await_count)
                self.assertTrue(all(call.args[1] == "turn_off" for call in agent.set_water_switches_state.await_args_list))
                agent.reschedule_current_task.assert_not_awaited()

class EnhancedVisualTests(unittest.IsolatedAsyncioTestCase):
    setUp=VisualWorkerTests.setUp
    tearDown=VisualWorkerTests.tearDown
    async def test_second_review_disagreement_cannot_discard_person(self):
        from dataclasses import replace
        self.worker.configs=(replace(self.config,review_model='visual/strong',daily_call_limit=5),)
        values=[verdict(visual_verdict='supported_false_positive',person_count=0,alternative_explanation='Una sombra de rama.'),verdict()]
        self.create.side_effect=[SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(v)))],usage=None) for v in values]
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        row=recent_cases(self.memory.conn)[0]
        self.assertEqual(row['result']['visual_verdict'],'inconclusive')
        self.assertEqual(self.create.await_count,2)
        first=self.memory.conn.execute('SELECT result_json FROM security_visual_reviews ORDER BY id LIMIT 1').fetchone()[0]
        self.assertEqual(json.loads(first)['visual_verdict'],'supported_false_positive')

    async def test_installation_checks_do_not_improve_detection_accuracy_statistics(self):
        from services.security_visual import enqueue_check,visual_metrics
        case_id=enqueue_check(self.memory.conn,self.config)
        set_case_label(self.memory.conn,case_id,'person')
        self.assertEqual(visual_metrics(self.memory.conn)['labeled_cases'],0)

    async def test_motion_candidate_is_not_mislabeled_as_native_person(self):
        from dataclasses import replace
        self.worker.configs=(replace(self.config,motion_entities=('binary_sensor.casa_motion',)),)
        self.worker.offer(event(entity='binary_sensor.casa_motion'))
        c=recent_cases(self.memory.conn)[0]['candidate']
        self.assertEqual(c['trigger_kind'],'motion')
        self.assertEqual(c['source'],'tapo_motion')

class ProviderContractTests(unittest.IsolatedAsyncioTestCase):
    setUp=VisualWorkerTests.setUp
    tearDown=VisualWorkerTests.tearDown
    async def test_invalid_primary_contract_uses_bounded_second_review(self):
        from dataclasses import replace
        self.worker.configs=(replace(self.config,review_model='visual/strong',daily_call_limit=5),)
        self.create.side_effect=[SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"wrong":true}'))],usage=None),SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(verdict())))],usage=None)]
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.assertEqual(self.create.await_count,2)
        self.assertEqual(recent_cases(self.memory.conn)[0]['result']['visual_verdict'],'person_visible')
    async def test_budget_cannot_be_bypassed_by_invalid_provider_contract(self):
        from dataclasses import replace
        self.worker.configs=(replace(self.config,review_model='visual/strong',daily_call_limit=1),)
        self.create.return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"wrong":true}'))],usage=None)
        self.worker.offer(frigate_event())
        await self.worker.process_one()
        self.assertEqual(self.create.await_count,1)
        self.assertEqual(recent_cases(self.memory.conn)[0]['result']['visual_verdict'],'inconclusive')

class SchemaNumericTests(unittest.TestCase):
    def test_json_schema_integer_accepts_integral_decimal_only(self):
        self.assertEqual(validate_verdict(json.dumps(verdict(person_count=1.0)),'1',[{'alignment':'matched'}])['person_count'],1)
        for count in [1.5,'1',True]:
            with self.subTest(count=count),self.assertRaises(ValueError):
                validate_verdict(json.dumps(verdict(person_count=count)),'1',[{'alignment':'matched'}])

class MotionFallbackTests(unittest.IsolatedAsyncioTestCase):
    setUp=VisualWorkerTests.setUp
    tearDown=VisualWorkerTests.tearDown
    async def test_motion_does_not_use_budget_when_native_person_detector_is_available(self):
        from dataclasses import replace
        self.worker.configs=(replace(self.config,motion_entities=('binary_sensor.casa_motion',)),)
        self.worker.offer(event(value='off',old='unavailable'))
        self.assertIsNone(self.worker.offer(event(entity='binary_sensor.casa_motion')))
        self.assertFalse(await self.worker.process_one())
        self.create.assert_not_awaited()
    async def test_motion_fallback_returns_if_native_detector_becomes_unavailable(self):
        from dataclasses import replace
        self.worker.configs=(replace(self.config,motion_entities=('binary_sensor.casa_motion',)),)
        self.worker.offer(event(value='off',old='unavailable'))
        self.worker.offer(event(value='unavailable',old='off'))
        self.assertIsNotNone(self.worker.offer(event(entity='binary_sensor.casa_motion')))
