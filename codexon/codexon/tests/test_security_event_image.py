import io,json,tempfile,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"examples"/"appdaemon"))
from SecurityEventImage import event_image
class Tests(unittest.TestCase):
 def test_exact_event_is_saved_and_notification_is_not_live(self):
  urls=[]
  def fetch(url,**kwargs):urls.append(url);return io.BytesIO(b'\xff\xd8\xff'+b'a'*200)
  with tempfile.TemporaryDirectory() as root:
   result=event_image({'frigate_names':['casa']},'frigate:casa:1234-a',root,opener=fetch)
   self.assertEqual(result['event_id'],'1234-a');self.assertTrue(result['image'].startswith('/local/security_alerts/'));self.assertEqual(len(urls),1);self.assertIn('/events/1234-a/snapshot.jpg',urls[0])
 def test_physical_sensor_uses_recent_matching_person(self):
  def fetch(url,**kwargs):
   if 'snapshot.jpg' in url:return io.BytesIO(b'\xff\xd8\xff'+b'a'*200)
   return io.BytesIO(json.dumps([{'id':'99-a','camera':'cam','label':'person','start_time':990,'end_time':995,'has_snapshot':True},{'id':'98-b','camera':'cam','label':'person','start_time':945,'end_time':950,'has_snapshot':True}]).encode())
  with tempfile.TemporaryDirectory() as root:self.assertEqual(event_image({'frigate_names':['cam']},'pir',root,now=1000,opener=fetch)['event_id'],'99-a')
 def test_no_matching_person_does_not_use_other_camera(self):
  fetch=lambda *a,**k:io.BytesIO(json.dumps([{'id':'99-a','camera':'other','label':'person','start_time':990,'has_snapshot':True}]).encode())
  with tempfile.TemporaryDirectory() as root:self.assertIsNone(event_image({'frigate_names':['cam']},'pir',root,now=1000,opener=fetch))
 def test_wrong_event_camera_is_rejected(self):
  with tempfile.TemporaryDirectory() as root:self.assertIsNone(event_image({'frigate_names':['cam']},'frigate:other:99-a',root))
 def test_invalid_jpeg_is_rejected(self):
  with tempfile.TemporaryDirectory() as root:
   with self.assertRaises(ValueError):event_image({'frigate_names':['cam']},'frigate:cam:99-a',root,opener=lambda *a,**k:io.BytesIO(b'error'))
if __name__=='__main__':unittest.main()
