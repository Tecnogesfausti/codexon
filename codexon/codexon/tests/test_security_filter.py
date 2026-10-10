import json,time,unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tests import test_security_visual as visual
from services.security_visual import SecurityVisualSupervisor,utc_now
from site_profile import SiteProfile

def request(worker,request_id='a'*32,**changes):
 now=time.time();after={'id':'event-1','camera':'casa','label':'person','start_time':now-1,'snapshot':{'frame_time':worker.frames_for_test[0]['frame_time']},'has_snapshot':True}
 data={'request_id':request_id,'camera':'casa','requested_at':utc_now(),'deadline':now+15,'after':after};data.update(changes)
 return {'event_type':'codexon_security_review_requested','data':data}

def response(**changes):
 return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(visual.verdict(**changes))))],usage=None)

class FilterBridgeTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  visual.VisualWorkerTests.setUp(self)
  self.agent.site_profile=SiteProfile(path=None,roles={'security.visual_filter':{'entity_id':'sensor.filter','enabled':True}})
  self.worker.filter_enabled=True;self.worker.publish_filter_result=AsyncMock();self.worker.frames_for_test=self.frames
 def tearDown(self):visual.VisualWorkerTests.tearDown(self)
 def false_responses(self):
  self.worker.configs=(replace(self.config,review_model='review/vision'),)
  v={'visual_verdict':'supported_false_positive','person_count':0,'alternative_explanation':'Rama visible en la caja del detector.'}
  self.create.side_effect=[response(**v),response(**v)]
 async def test_two_models_same_fresh_event_photo_can_filter(self):
  self.false_responses();self.worker.offer(request(self.worker));await self.worker.process_one();await self.worker.deliver_filter_results()
  payload=self.worker.publish_filter_result.call_args.args[0]
  self.assertEqual(payload['decision'],'suppress_false_positive');self.assertEqual(payload['proof']['distinct_models'],2)
 async def test_single_model_never_suppresses(self):
  self.create.return_value=response(visual_verdict='supported_false_positive',person_count=0,alternative_explanation='Rama visible.')
  self.worker.offer(request(self.worker));await self.worker.process_one();await self.worker.deliver_filter_results();self.assertEqual(self.worker.publish_filter_result.call_args.args[0]['decision'],'notify')
 async def test_disagreement_preserves_notification(self):
  self.false_responses();self.create.side_effect[1] if isinstance(self.create.side_effect,list) else None
  self.create.side_effect=[response(visual_verdict='supported_false_positive',person_count=0,alternative_explanation='Rama visible.'),response()]
  self.worker.offer(request(self.worker));await self.worker.process_one();await self.worker.deliver_filter_results();self.assertEqual(self.worker.publish_filter_result.call_args.args[0]['decision'],'notify')
 async def test_expired_request_never_publishes_late_suppression(self):
  self.false_responses();self.worker.offer(request(self.worker));await self.worker.process_one();self.memory.conn.execute("UPDATE security_visual_filter_requests SET deadline=?",(time.time()-1,));self.memory.conn.commit();await self.worker.deliver_filter_results();self.worker.publish_filter_result.assert_not_awaited()
 async def test_missing_frame_time_and_old_snapshot_keep_notification(self):
  self.false_responses();self.worker.offer(request(self.worker));await self.worker.process_one();self.memory.conn.execute('UPDATE security_visual_filter_requests SET frame_time=?',(time.time()-60,));self.memory.conn.commit();await self.worker.deliver_filter_results();self.assertEqual(self.worker.publish_filter_result.call_args.args[0]['decision'],'notify')
 async def test_human_person_label_vetoes_filter(self):
  self.false_responses();self.worker.offer(request(self.worker));await self.worker.process_one();self.memory.conn.execute("UPDATE security_visual_cases SET label='legitimate'");self.memory.conn.commit();await self.worker.deliver_filter_results();self.assertEqual(self.worker.publish_filter_result.call_args.args[0]['decision'],'notify')
 async def test_different_models_must_review_identical_photo(self):
  self.false_responses();self.worker.offer(request(self.worker));await self.worker.process_one();frames=[{**self.frames[0],'sha256':'different','bytes':None}];frames[0].pop('bytes');self.memory.conn.execute('UPDATE security_visual_reviews SET frames_json=? WHERE model=?',(json.dumps(frames),'review/vision'));self.memory.conn.commit();await self.worker.deliver_filter_results();self.assertEqual(self.worker.publish_filter_result.call_args.args[0]['decision'],'notify')
 async def test_missing_or_thumbnail_evidence_never_suppresses(self):
  self.frames[0]['source']='frigate_thumbnail';self.false_responses();self.worker.offer(request(self.worker));await self.worker.process_one();await self.worker.deliver_filter_results();self.assertEqual(self.worker.publish_filter_result.call_args.args[0]['decision'],'notify')
 async def test_duplicate_request_does_not_duplicate_reviews(self):
  e=request(self.worker);self.worker.offer(e);self.worker.offer(e);self.assertEqual(self.memory.conn.execute('SELECT count(*) FROM security_visual_filter_requests').fetchone()[0],1);await self.worker.process_one();self.create.assert_awaited_once()
 async def test_invalid_and_nonconfigured_requests_rejected(self):
  for changes in [{'request_id':'bad'},{'camera':'wrong'},{'deadline':time.time()+100},{'after':{'id':'event-1','label':'person','camera':'wrong'}}]:
   self.assertIsNone(self.worker.offer(request(self.worker,**changes)))
  self.assertEqual(self.memory.conn.execute('SELECT count(*) FROM security_visual_filter_requests').fetchone()[0],0)
 async def test_failed_result_publish_retains_request_and_appdaemon_owns_timeout(self):
  self.worker.offer(request(self.worker));await self.worker.process_one();self.worker.publish_filter_result.side_effect=RuntimeError('HA offline');await self.worker.deliver_filter_results();self.assertEqual(self.memory.conn.execute('SELECT status FROM security_visual_filter_requests').fetchone()[0],'pending')
 async def test_restart_retains_request_for_fail_open_reply(self):
  self.worker.offer(request(self.worker));restarted=SecurityVisualSupervisor(self.agent,configs=(self.config,),log=self.worker.log);restarted.publish_filter_result=AsyncMock();self.memory.conn.execute("UPDATE security_visual_cases SET status='reviewed'");self.memory.conn.commit();await restarted.deliver_filter_results();self.assertEqual(restarted.publish_filter_result.call_args.args[0]['decision'],'notify')
 async def test_real_outcome_recorded_independently_from_model_prediction(self):
  self.worker.offer(request(self.worker));self.worker.offer({'event_type':'codexon_security_filter_outcome','data':{'request_id':'a'*32,'camera':'casa','outcome':'timeout'}});self.assertEqual(self.memory.conn.execute('SELECT outcome FROM security_visual_filter_requests').fetchone()[0],'timeout')

if __name__=='__main__':unittest.main()
