import json,time,unittest
from unittest.mock import Mock
from site_profile import SiteProfile
from services.security_visual import ArmingGate,SecurityVisualSupervisor,enqueue_check
from tests import test_security_visual as visual_tests
from tests.test_security_visual import event,frigate_event

TOTAL='switch.total';NIGHT='input_boolean.night'
def profile(night_cameras=None):
 return SiteProfile(path=None,roles={'security.visual_policy':{'entity_id':TOTAL,'enabled':True,'night_arm_entity':NIGHT,'night_cameras':['casa'] if night_cameras is None else night_cameras}})

class ArmingPolicyTests(unittest.TestCase):
 def setUp(self):
  from services.security_visual import CameraConfig
  self.cameras=(CameraConfig('casa','camera.casa',('binary_sensor.casa_person',)),CameraConfig('oficina','camera.oficina',('binary_sensor.oficina_person',)))
  self.gate=ArmingGate(profile(),self.cameras);self.now=time.time();self.changed=self.now-100
 def states(self,total,night):
  self.gate.update(TOTAL,total,self.changed);self.gate.update(NIGHT,night,self.changed)
 def test_disarmed_stops_all_automatic_reviews(self):
  self.states('off','off')
  for c in ['casa','oficina']:self.assertFalse(self.gate.decision(c,self.now)['allowed'])
 def test_total_covers_all_cameras(self):
  self.states('on','off')
  for c in ['casa','oficina']:self.assertEqual(self.gate.decision(c,self.now)['mode'],'total')
 def test_night_only_covers_explicit_cameras(self):
  self.states('off','on');self.assertEqual(self.gate.decision('casa',self.now)['mode'],'night');self.assertFalse(self.gate.decision('oficina',self.now)['allowed'])
 def test_unknown_does_not_disable_security(self):
  for total,night,camera in [('unknown','off','oficina'),('off','unavailable','casa')]:
   self.states(total,night);self.assertEqual(self.gate.decision(camera,self.now)['mode'],'unknown');self.assertTrue(self.gate.decision(camera,self.now)['allowed'])
 def test_missing_history_is_not_disarmed(self):
  self.states('off','off');self.assertTrue(self.gate.decision('casa',self.changed-5)['allowed'])
 def test_event_time_survives_later_disarming(self):
  self.states('on','off');self.gate.update(TOTAL,'off',self.now-1)
  self.assertEqual(self.gate.decision('casa',self.now-2)['mode'],'total');self.assertFalse(self.gate.decision('casa',self.now)['allowed'])
 def test_old_disarmed_event_not_made_armed_by_later_change(self):
  self.states('off','off');self.gate.update(TOTAL,'on',self.now-1)
  self.assertFalse(self.gate.decision('casa',self.now-2)['allowed']);self.assertTrue(self.gate.decision('casa',self.now)['allowed'])
 def test_stale_states_are_unknown(self):
  self.states('off','off');self.gate.observed={TOTAL:self.now-91,NIGHT:self.now-91};self.assertTrue(self.gate.decision('casa',self.now)['allowed'])
 def test_disconnect_and_recovery(self):
  self.states('off','off');self.gate.disconnect();self.assertTrue(self.gate.decision('casa',time.time())['allowed'])
  self.gate.update(TOTAL,'off',self.changed,polled=True);self.gate.update(NIGHT,'off',self.changed,polled=True)
  self.assertFalse(self.gate.decision('casa',time.time())['allowed'])
 def test_poll_gap_is_unknown_instead_of_false_disarmed_history(self):
  self.states('off','off');self.gate.observed[TOTAL]=self.now-50;self.gate.update(TOTAL,'on',self.now-10,polled=True)
  self.assertEqual(self.gate.decision('casa',self.now-25)['mode'],'unknown')
 def test_invalid_configuration_rejected(self):
  for binding in [{'entity_id':'switch.total','night_arm_entity':'camera.bad','night_cameras':['casa']},{'entity_id':TOTAL,'night_arm_entity':NIGHT,'night_cameras':['missing']}]:
   binding['enabled']=True
   with self.assertRaises(ValueError):ArmingGate(SiteProfile(path=None,roles={'security.visual_policy':binding}),self.cameras)

class ArmedWorkerTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  visual_tests.VisualWorkerTests.setUp(self)
  self.agent.site_profile=profile();self.worker.arming=ArmingGate(self.agent.site_profile,self.worker.configs)
  self.set_states('off','off')
 def tearDown(self):visual_tests.VisualWorkerTests.tearDown(self)
 def set_states(self,total,night):
  self.worker.arming.update(TOTAL,total,time.time()-100);self.worker.arming.update(NIGHT,night,time.time()-100)
 async def test_off_creates_no_case_capture_or_call(self):
  self.assertIsNone(self.worker.offer(frigate_event()));self.assertFalse(await self.worker.process_one());self.worker.capture.assert_not_awaited();self.create.assert_not_awaited();self.assertEqual(self.memory.conn.execute('SELECT count(*) FROM security_visual_cases').fetchone()[0],0)
 async def test_pending_legacy_event_is_checked_before_capture(self):
  self.set_states('on','off');cid=self.worker.offer(frigate_event());row=self.memory.conn.execute('SELECT candidate_json FROM security_visual_cases WHERE id=?',(cid,)).fetchone();candidate=json.loads(row[0]);candidate.pop('arming');self.memory.conn.execute('UPDATE security_visual_cases SET candidate_json=? WHERE id=?',(json.dumps(candidate),cid));self.memory.conn.commit();self.set_states('off','off')
  self.assertTrue(await self.worker.process_one());self.worker.capture.assert_not_awaited();self.create.assert_not_awaited()
 async def test_armed_event_is_not_lost_when_disarmed_in_queue(self):
  self.set_states('on','off');self.worker.offer(frigate_event());self.worker.offer(event(TOTAL,'off','on'));await self.worker.process_one();self.create.assert_awaited_once()
 async def test_manual_button_still_works_disarmed(self):
  enqueue_check(self.memory.conn,self.config);await self.worker.process_one();self.create.assert_awaited_once()
 async def test_unknown_state_preserves_review_and_diagnostic(self):
  self.worker.arming=ArmingGate(profile(),self.worker.configs);self.worker.publish_arming();self.assertTrue(json.loads(self.memory.get_setting('security_visual.arming'))['unknown']);self.worker.offer(frigate_event());await self.worker.process_one();self.create.assert_awaited_once()
 async def test_arming_reviews_person_already_present(self):
  self.assertIsNone(self.worker.offer(event()));self.worker.offer(event(TOTAL,'on','off'));await self.worker.process_one();self.create.assert_awaited_once()
 async def test_disarmed_repeated_frigate_poll_does_not_grow_skip_count(self):
  e=frigate_event()
  self.worker.offer(e);self.worker.offer(e);self.assertEqual(json.loads(self.memory.get_setting('security_visual.arming_skipped'))['casa']['count'],1)
 async def test_updated_frame_after_arming_can_review_ongoing_event(self):
  self.set_states('off','off');start=time.time()-60;self.worker.arming.update(TOTAL,'on',time.time()-10);self.worker.offer(frigate_event(start_time=start,snapshot={'frame_time':time.time()}));await self.worker.process_one();self.create.assert_awaited_once()
 async def test_refresh_failure_keeps_review_available(self):
  await self.worker.refresh_arming();self.assertTrue(self.worker.arming.decision('casa',time.time())['allowed']);self.assertTrue(json.loads(self.memory.get_setting('security_visual.arming'))['unknown'])

if __name__=='__main__':unittest.main()
