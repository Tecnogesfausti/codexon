import json,time,unittest
from unittest.mock import AsyncMock,patch
import codexon_web
from tests import test_security_visual as legacy
from tests.test_security_arming import profile,TOTAL
from services.security_visual import ArmingGate,model_setting,inconclusive

class SimulacroWorkerTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):legacy.VisualWorkerTests.setUp(self)
 def tearDown(self):legacy.VisualWorkerTests.tearDown(self)
 def activate(self):
  self.worker.offer(legacy.event('sensor.camera_security_simulacro','en_curso',started_at=time.time(),ends_at=time.time()+120,general=[],nocturna=[]))
 async def test_drill_merges_camera_sources_and_calls_ai(self):
  self.activate();self.worker.refresh_environment=AsyncMock(return_value={});case=self.worker.offer(legacy.event());self.assertEqual(self.worker.offer(legacy.frigate_event()),case);self.assertTrue(await self.worker.process_one());self.worker.capture.assert_awaited_once();self.create.assert_awaited_once()
 async def test_existing_pending_case_skips_capture(self):
  self.worker.offer(legacy.frigate_event());self.activate();self.assertTrue(await self.worker.process_one());self.worker.capture.assert_not_called();self.create.assert_not_called()
 async def test_arm_changes_and_native_state_cache_continue_during_drill(self):
  self.worker.arming=ArmingGate(profile(),(self.config,));self.activate();self.worker.offer(legacy.event(TOTAL,'on'));self.worker.offer(legacy.event());self.assertEqual(self.worker.arming.state_at(TOTAL,time.time()),'on');self.assertEqual(model_setting(self.memory.conn,'security_visual.native_states.casa')['binary_sensor.casa_person'],'on');self.create.assert_not_called()
 async def test_second_and_fallback_calls_paused(self):
  self.activate();first=inconclusive('1','test');await self.worker.second_review(1,self.config,{},self.frames,first,contract_error=True);await self.worker.fallback_review(1,self.config,{},self.frames);self.create.assert_not_called()
 async def test_drill_start_during_capture_prevents_call(self):
  self.worker.offer(legacy.frigate_event())
  async def capture(*args):self.activate();return self.frames
  self.worker.capture.side_effect=capture;await self.worker.process_one();self.create.assert_not_called()
 async def test_deadline_does_not_leave_worker_permanently_paused(self):
  self.worker.store_simulacro({'state':'en_curso','attributes':{'ends_at':time.time()-1}});self.assertFalse(self.worker.simulacro_active());self.worker.offer(legacy.frigate_event());await self.worker.process_one();self.create.assert_called_once()
 async def test_completion_state_restarts_processing(self):
  self.activate();self.worker.offer(legacy.event('sensor.camera_security_simulacro','finalizado',last_message='General: presencia Casa'));self.assertFalse(self.worker.simulacro_active());self.worker.offer(legacy.frigate_event());await self.worker.process_one();self.create.assert_called_once();self.assertEqual(model_setting(self.memory.conn,'security_visual.simulacro')['last_message'],'General: presencia Casa')

class SimulacroWebTests(unittest.IsolatedAsyncioTestCase):
 async def test_fixed_helper_no_sensor_changes(self):
  with patch.object(codexon_web,'visual_helper_service',new_callable=AsyncMock) as service:
   result=await codexon_web.api_visual_simulacro();service.assert_awaited_once_with('input_button','press',{'entity_id':'input_button.simulacro'});self.assertTrue(result['ok'])
 async def test_ha_failure_not_reported_as_success(self):
  with patch.object(codexon_web,'visual_helper_service',new_callable=AsyncMock,side_effect=RuntimeError('offline')):
   with self.assertRaises(RuntimeError):await codexon_web.api_visual_simulacro()

if __name__=='__main__':unittest.main()
