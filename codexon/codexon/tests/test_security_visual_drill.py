import json,time,unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from tests import test_security_visual as fixture
from tests.test_security_arming import profile,TOTAL
from services.security_visual import ArmingGate,recent_cases,visual_metrics

class VisualDrillTests(unittest.IsolatedAsyncioTestCase):
 setUp=fixture.VisualWorkerTests.setUp
 tearDown=fixture.VisualWorkerTests.tearDown
 def start(self):
  self.agent.ha_base_url='http://ha.test';self.agent.ha_token='test-only';self.started=time.time();self.ends=self.started+120
  self.worker.offer(fixture.event('sensor.camera_security_simulacro','en_curso',started_at=self.started,ends_at=self.ends))
  self.worker.refresh_environment=AsyncMock(return_value={'weather.aemet':{'state':'rainy'},'sun.sun':{'state':'above_horizon'}})
 def state(self,value):self.worker.offer(fixture.event('sensor.camera_security_simulacro',value,started_at=self.started,ends_at=self.ends))
 def offer(self):
  e=fixture.frigate_event();self.frames[0]['frame_time']=e['data']['new_state']['attributes']['after']['snapshot']['frame_time'];return self.worker.offer(e)
 def http(self):
  self.post=AsyncMock(return_value=SimpleNamespace(raise_for_status=Mock()));context=AsyncMock();context.__aenter__.return_value=SimpleNamespace(post=self.post)
  return patch('services.security_visual.httpx_module',return_value=SimpleNamespace(AsyncClient=Mock(return_value=context)))
 async def test_unarmed_drill_reviews_and_records_weather(self):
  self.worker.arming=ArmingGate(profile(),(self.config,));self.worker.arming.update(TOTAL,'off',time.time()-10);self.start();self.offer();self.assertTrue(await self.worker.process_one())
  c=recent_cases(self.memory.conn)[0];self.assertEqual(c['candidate']['kind'],'simulacro');self.assertEqual(c['candidate']['light_context'],'day');self.assertEqual(c['candidate']['environment']['weather.aemet']['state'],'rainy');self.assertEqual(c['result']['visual_verdict'],'person_visible');self.create.assert_awaited_once()
  with self.http():await self.worker.deliver_simulacro_results();await self.worker.deliver_simulacro_results()
  self.post.assert_awaited_once();self.assertTrue(self.post.call_args.args[0].endswith('/api/events/codexon_security_simulacro_visual_result'));self.assertNotIn('tools',self.create.call_args.kwargs)
 async def test_tapo_frigate_share_review(self):
  self.start();case=self.worker.offer(fixture.event());self.assertFalse(await self.worker.process_one());self.assertEqual(self.offer(),case);await self.worker.process_one();self.create.assert_awaited_once();self.worker.capture.assert_awaited_once()
 async def test_single_model_cannot_discard(self):
  self.start();self.create.return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(fixture.verdict(visual_verdict='supported_false_positive',person_count=0,alternative_explanation='Gato visible.',observations=['Gato en la caja.']))))],usage=None);self.offer();await self.worker.process_one()
  with self.http():await self.worker.deliver_simulacro_results()
  self.assertEqual(self.post.call_args.kwargs['json']['result']['visual_verdict'],'inconclusive');self.assertEqual(recent_cases(self.memory.conn)[0]['result']['visual_verdict'],'inconclusive')
 async def test_two_models_matching_animal_frame_allow_discard(self):
  self.start();self.worker.configs=(replace(self.config,review_model='visual/second'),);v=fixture.verdict(visual_verdict='supported_false_positive',person_count=0,alternative_explanation='Gato visible.',observations=['Gato dentro de la caja.']);self.create.return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(v)))],usage=None);self.offer();await self.worker.process_one()
  with self.http():await self.worker.deliver_simulacro_results()
  p=self.post.call_args.kwargs['json'];self.assertEqual(p['result']['visual_verdict'],'supported_false_positive');self.assertEqual(p['proof']['distinct_models'],2);self.assertEqual(self.create.await_count,2)
 async def test_cancellation_during_capture_prevents_ia_and_results(self):
  self.start();self.offer()
  async def capture(*args):self.state('cancelado');return self.frames
  self.worker.capture.side_effect=capture;await self.worker.process_one()
  with self.http():await self.worker.deliver_simulacro_results()
  self.create.assert_not_awaited();self.post.assert_not_awaited()
 async def test_finished_accepts_own_late_result_cancelled_does_not(self):
  self.start();self.offer();await self.worker.process_one();self.state('finalizado')
  with self.http():await self.worker.deliver_simulacro_results()
  self.post.assert_awaited_once();self.state('cancelado');self.assertFalse(self.worker.simulacro_case_allowed(recent_cases(self.memory.conn)[0]['candidate']))
 async def test_new_drill_rejects_old_result(self):
  self.start();self.offer();await self.worker.process_one();self.start()
  with self.http():await self.worker.deliver_simulacro_results()
  self.post.assert_not_awaited()
 async def test_weather_groups_require_human_label(self):
  self.start();case=self.offer();await self.worker.process_one();self.assertEqual(visual_metrics(self.memory.conn)['weather_groups'],[]);self.memory.conn.execute('UPDATE security_visual_cases SET label=? WHERE id=?',('person',case));self.memory.conn.commit();self.assertEqual(visual_metrics(self.memory.conn)['weather_groups'][0]['weather_context'],'rainy')
 async def test_budget_exhaustion_stays_inconclusive(self):
  self.start();self.worker.configs=(replace(self.config,daily_call_limit=0),);self.offer();await self.worker.process_one()
  with self.http():await self.worker.deliver_simulacro_results()
  self.create.assert_not_awaited();self.assertEqual(self.post.call_args.kwargs['json']['result']['visual_verdict'],'inconclusive')
