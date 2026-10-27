import asyncio,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from services.security_visual_sequence import sequence_times,filter_evidence_valid,capture_sequence,_image

class EvidenceTests(unittest.TestCase):
 def frames(self):
  common={'event_id':'event','camera':'casa','event_start':90.,'event_end':110.,'alignment':'matched'}
  return [{'source':'frigate_event','sha256':'a','frame_time':100.,**common},
   {'source':'frigate_event_crop','sha256':'c','frame_time':100.,'parent_sha256':'a','timestamp_basis':'snapshot_response_header',**common},
   {'source':'frigate_recording','sha256':'r','frame_time':99.,'anchor_sha256':'a','time_uncertainty_seconds':1,'timestamp_basis':'recording_seek',**common}]
 def test_three_distinct_times_inside_event(self):
  self.assertEqual(sequence_times(100.,90.,110.,120.),[99.,101.])
  self.assertEqual(sequence_times(100.,100.,110.,120.),[101.,102.])
  self.assertEqual(sequence_times(100.,100.,None,100.5),[])
 def test_multiframe_proof_keeps_original_anchor(self):
  f=self.frames();self.assertTrue(filter_evidence_valid(f,'event',100.));self.assertFalse(filter_evidence_valid(f[1:],'event',100.))
 def test_wrong_camera_event_parent_time_or_quality_cannot_suppress(self):
  for index,key,value in [(1,'event_id','other'),(1,'camera','other'),(1,'parent_sha256','wrong'),(1,'frame_time',101.),(2,'alignment','unknown'),(2,'time_uncertainty_seconds',2),(2,'anchor_sha256','wrong'),(2,'frame_time',105.)]:
   with self.subTest(key=key):
    f=self.frames();f[index][key]=value;self.assertFalse(filter_evidence_valid(f,'event',100.))
 def test_legacy_single_photo_proof_unchanged(self):
  self.assertTrue(filter_evidence_valid([{'source':'frigate_event','alignment':'matched','frame_time':100.,'sha256':'a'}],'event',100.))

class CaptureTests(unittest.IsolatedAsyncioTestCase):
 async def test_native_header_is_numeric_and_controls_original_time(self):
  async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'\xff\xd8\xff'+b'x'*150,headers={'Content-Type':'image/jpeg','X-Frame-Time':'100.5'}))) as client:
   result=await _image(client,'http://frigate/snapshot',1)
   self.assertEqual(result[2],100.5)
 async def run_capture(self,missing=False,race=False):
  with tempfile.TemporaryDirectory() as folder:
   owner=SimpleNamespace(image_dir=Path(folder),memory=SimpleNamespace(set_setting=lambda *x:None))
   config=SimpleNamespace(frigate_base_url='http://frigate',frigate_cameras=('casa',))
   event={'id':'event','camera':'casa','start_time':90.,'end_time':110.}
   original=httpx.AsyncClient
   async def handler(request):
    if request.url.path=='/api/events/event':return httpx.Response(200,json=event)
    crop=request.url.params.get('crop')=='1';record='/recordings/' in request.url.path
    if record and missing:return httpx.Response(404)
    header={'Content-Type':'image/jpeg','X-Frame-Time':str(101. if crop and race else 100.)}
    return httpx.Response(200,content=b'\xff\xd8\xff'+str(request.url).encode()*15,headers=header)
   def client(**kwargs):return original(**kwargs,transport=httpx.MockTransport(handler))
   with patch('services.security_visual_sequence.httpx.AsyncClient',side_effect=client):
    frames=await capture_sequence(owner,1,{'event_id':'event'},config)
   self.assertTrue(all(Path(f['path']).exists() for f in frames));return frames
 async def test_complete_sequence_and_crop(self):
  f=await self.run_capture();self.assertEqual(len(f),4);self.assertTrue(filter_evidence_valid(f,'event',100.))
 async def test_missing_recording_keeps_original_and_crop(self):
  f=await self.run_capture(missing=True);self.assertEqual(len(f),2);self.assertTrue(filter_evidence_valid(f,'event',100.))
 async def test_moving_snapshot_never_ties_wrong_crop_to_original(self):
  f=await self.run_capture(race=True);self.assertEqual(len(f),3);self.assertTrue(all(x['source']!='frigate_event_crop' for x in f))
if __name__=='__main__':unittest.main()

from tests.test_security_filter import FilterBridgeTests,request
class SequenceFilterTests(unittest.IsolatedAsyncioTestCase):
 setUp=FilterBridgeTests.setUp
 tearDown=FilterBridgeTests.tearDown
 false_responses=FilterBridgeTests.false_responses
 async def prepare(self):
  self.false_responses();t=self.frames[0]['frame_time']
  self.frames[0].update(event_id='event-1',camera='casa',event_start=t-4,event_end=t+4)
  self.frames.append({**self.frames[0],'source':'frigate_event_crop','sha256':'crop','parent_sha256':self.frames[0]['sha256'],'timestamp_basis':'snapshot_response_header'})
  self.frames.append({**self.frames[0],'source':'frigate_recording','sha256':'record','frame_time':t-1,'anchor_sha256':self.frames[0]['sha256'],'time_uncertainty_seconds':1,'timestamp_basis':'recording_seek'})
  self.worker.offer(request(self.worker));await self.worker.process_one()
 async def test_same_full_sequence_by_two_models_can_filter(self):
  await self.prepare();await self.worker.deliver_filter_results()
  result=self.worker.publish_filter_result.call_args.args[0];self.assertEqual(result['decision'],'suppress_false_positive');self.assertEqual(result['proof']['distinct_models'],2)
 async def test_substituted_crop_in_one_review_cannot_filter(self):
  await self.prepare()
  row=self.memory.conn.execute('SELECT id,frames_json FROM security_visual_reviews ORDER BY id DESC LIMIT 1').fetchone();frames=json.loads(row['frames_json']);frames[-2]['parent_sha256']='unrelated'
  self.memory.conn.execute('UPDATE security_visual_reviews SET frames_json=? WHERE id=?',(json.dumps(frames),row['id']));self.memory.conn.commit()
  await self.worker.deliver_filter_results();self.assertEqual(self.worker.publish_filter_result.call_args.args[0]['decision'],'notify')
