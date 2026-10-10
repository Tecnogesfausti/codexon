import datetime as dt,json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from fastapi import HTTPException
from codexon import MemoryStore
import codexon_web as web
from services.security_image_storage import cleanup_images,retention_days,validated_days,date_range,put_setting,RETENTION_KEY,RETENTION_ENTITY,FROM_ENTITY,TO_ENTITY,SHOW_ENTITY,offer_control,sync_controls
from services.security_visual import ensure_security_schema,recent_cases

class StorageTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.memory=MemoryStore(self.root/'db');self.conn=self.memory.conn;ensure_security_schema(self.conn)
 def tearDown(self):self.conn.close();self.tmp.cleanup()
 def photo(self,number,age):
  path=self.root/'security_visual_images'/f'{number}-{"a"*64}.jpg'
  path.parent.mkdir(exist_ok=True);path.write_bytes(b'photo');now=time.time();os.utime(path,(now-age*86400,now-age*86400));return path
 def test_default_and_invalid_retention_values(self):
  self.assertEqual(retention_days(self.conn),15)
  for value in [True,False,0,91,1.5,'nan','inf',None]:
   with self.subTest(value=value),self.assertRaises(ValueError):validated_days(value)
  self.assertEqual(validated_days('15.0'),15)
 def test_only_managed_expired_photos_are_deleted_and_metrics_are_preserved(self):
  old=self.photo(1,16);recent=self.photo(2,14)
  foreign=self.root/'important.jpg';foreign.write_bytes(b'keep')
  link=old.with_name('3-'+('b'*64)+'.jpg');link.symlink_to(foreign)
  unrelated=old.with_name('notes.txt');unrelated.write_text('keep')
  report=cleanup_images(old.parent,15)
  self.assertEqual(report['deleted_files'],1);self.assertEqual(report['freed_bytes'],5)
  self.assertFalse(old.exists());self.assertTrue(recent.exists());self.assertTrue(foreign.exists());self.assertTrue(link.is_symlink());self.assertTrue(unrelated.exists())
 def test_selected_dates_include_last_day_and_support_dst(self):
  selected=date_range('2026-10-25','2026-10-25','Europe/Madrid')
  self.assertEqual(selected['end_timestamp']-selected['start_timestamp'],25*3600)
  default=date_range(now=dt.datetime(2026,10,5,10,tzinfo=dt.UTC))
  self.assertEqual((default['from_date'],default['to_date']),('2026-10-04','2026-10-05'))
  for start,end in [('2026-10-06','2026-10-05'),('2026-02-30','2026-03-01'),('20261005','2026-10-05'),('2026-10-05',None)]:
   with self.subTest(start=start),self.assertRaises(ValueError):date_range(start,end)
 def test_appdaemon_cleanup_only_removes_expired_snapshots_of_configured_cameras(self):
  archive=self.root/'media';archive.mkdir();now=time.time()
  names=['security_camara_casa_20260901_120000.jpg','security_camara_oficina_20260901_120000.jpg','camara_casa_20260901_120000.jpg','security_camara_casa_20261300_120000.jpg']
  for name in names:
   path=archive/name;path.write_bytes(b'photo');os.utime(path,(now-16*86400,now-16*86400))
  report=cleanup_images(self.root/'security_visual_images',15,archive_root=archive,archive_cameras=['camara_casa'])
  self.assertEqual(report['deleted_files'],1);self.assertFalse((archive/names[0]).exists())
  self.assertTrue(all((archive/name).exists() for name in names[1:]))
 def test_sql_date_filter_is_applied_before_limit_and_midnight_end_is_excluded(self):
  selected=date_range('2026-10-05','2026-10-05')
  for index,seconds in enumerate([selected['start_timestamp']-1,selected['start_timestamp'],selected['end_timestamp']-1,selected['end_timestamp']]):
   stamp=dt.datetime.fromtimestamp(seconds,dt.UTC).isoformat()
   self.conn.execute('INSERT INTO security_visual_cases(source_key,camera,detected_at,received_at,candidate_json,result_json) VALUES(?,?,?,?,?,?)',(str(index),'casa',stamp,stamp,json.dumps({'sources':['tapo']}),json.dumps({'visual_verdict':'person_visible'})))
  self.conn.commit();rows=recent_cases(self.conn,20,start_timestamp=selected['start_timestamp'],end_timestamp=selected['end_timestamp'])
  self.assertEqual([r['id'] for r in rows],[3,2])
 def test_external_home_assistant_controls_update_policy_and_browse_selection(self):
  sync_controls(self.conn,{RETENTION_ENTITY:{'state':'15.0'},FROM_ENTITY:{'state':'2026-10-04'},TO_ENTITY:{'state':'2026-10-05'}})
  event={'event_type':'state_changed','data':{'entity_id':RETENTION_ENTITY,'old_state':{'state':'15.0'},'new_state':{'state':'20.0'}}}
  self.assertTrue(offer_control(self.conn,event));self.assertEqual(retention_days(self.conn),20)
  event['data'].update(entity_id=SHOW_ENTITY,old_state={'state':'unknown'},new_state={'state':dt.datetime.now(dt.UTC).isoformat()})
  self.assertTrue(offer_control(self.conn,event));selection=json.loads(self.memory.get_setting('security_visual.browse_selection'))
  self.assertEqual(selection['from_date'],'2026-10-04')
 def test_expired_images_are_marked_unavailable_but_analysis_remains(self):
  photo=self.photo(1,16)
  self.conn.execute('INSERT INTO security_visual_cases(source_key,camera,detected_at,received_at,candidate_json,result_json) VALUES(?,?,?,?,?,?)',('a','casa',dt.datetime.now(dt.UTC).isoformat(),dt.datetime.now(dt.UTC).isoformat(),'{"sources":["tapo"]}','{"visual_verdict":"person_visible"}'))
  self.conn.execute('INSERT INTO security_visual_reviews(case_id,started_at,frames_json) VALUES(?,?,?)',(1,dt.datetime.now(dt.UTC).isoformat(),json.dumps([{'path':str(photo),'media_type':'image/jpeg'}])));self.conn.commit()
  self.assertTrue(recent_cases(self.conn)[0]['reviews'][0]['frames'][0]['available'])
  cleanup_images(photo.parent,15)
  row=recent_cases(self.conn)[0];self.assertFalse(row['reviews'][0]['frames'][0]['available']);self.assertEqual(row['result']['visual_verdict'],'person_visible')

class StorageApiTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.db=patch.object(web,'DB_PATH',self.root/'memory.sqlite3');self.profile=patch.object(web,'SITE_PROFILE_PATH',self.root/'site.yaml');self.db.start();self.profile.start()
  self.ha=patch.object(web,'visual_helper_service',new_callable=AsyncMock);self.service=self.ha.start()
 async def asyncTearDown(self):self.ha.stop();self.profile.stop();self.db.stop();self.tmp.cleanup()
 async def test_saving_days_updates_helper_and_local_cleanup_policy(self):
  report=await web.api_visual_storage({'retention_days':15})
  self.service.assert_awaited_once_with('input_number','set_value',{'entity_id':RETENTION_ENTITY,'value':15})
  self.assertEqual(report['retention_days'],15);self.assertEqual(web.api_security_visual()['image_retention_days'],15)
 async def test_browse_updates_two_dates_and_button_without_llm(self):
  result=await web.api_visual_browse({'from_date':'2026-10-04','to_date':'2026-10-05'})
  self.assertEqual(self.service.await_count,3);self.assertEqual(result['selection']['from_date'],'2026-10-04')
  self.assertEqual(self.service.await_args_list[-1].args,('input_button','press',{'entity_id':SHOW_ENTITY}))
 async def test_invalid_ranges_and_days_do_not_change_helpers(self):
  with self.assertRaises(HTTPException):await web.api_visual_storage({'retention_days':1.5})
  with self.assertRaises(HTTPException):await web.api_visual_browse({'from_date':'2026-10-06','to_date':'2026-10-05'})
  self.service.assert_not_awaited()
