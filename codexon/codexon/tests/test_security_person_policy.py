import asyncio,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from codexon import MemoryStore
from services.security_visual import SecurityVisualSupervisor,camera_configs,ensure_security_schema,utc_now
from services.home_monitor import HomeMonitor
from site_profile import SiteProfile

class PersonPolicyTests(unittest.IsolatedAsyncioTestCase):
 async def test_six_person_only_cameras_ignore_motion_even_with_native_detector_unavailable(self):
  profile=SiteProfile(path=None,roles={f'security.visual.camera{i}':{'enabled':True,'entity_id':f'camera.camera{i}','presence_entities':[f'binary_sensor.camera{i}_person'],'frigate_cameras':[f'camera{i}'],'cooldown_seconds':120,'daily_call_limit':20 if i==0 else 10} for i in range(6)})
  configs=camera_configs(profile);self.assertEqual(len(configs),6)
  self.assertEqual(sum(c.daily_call_limit for c in configs),70)
  with tempfile.TemporaryDirectory() as d:
   memory=MemoryStore(Path(d)/'db');agent=SimpleNamespace(memory=memory,site_profile=profile)
   worker=SecurityVisualSupervisor(agent,configs=configs,log=Mock())
   for c in configs:
    self.assertFalse(c.motion_entities);self.assertEqual(c.cooldown_seconds,120)
    memory.set_setting('security_visual.native_states.'+c.name,json.dumps({e:'unavailable' for e in c.presence_entities}))
    for suffix in ('motion_alarm','cell_motion_detection'):
     event={'event_type':'state_changed','time_fired':utc_now(),'data':{'entity_id':c.entity_id.replace('camera.','binary_sensor.').removesuffix('_sd_stream')+'_'+suffix,'old_state':{'state':'off'},'new_state':{'state':'on'}}}
     self.assertIsNone(worker.offer(event))
    event['data']['entity_id']=c.presence_entities[0]
    self.assertIsNotNone(worker.offer(event))
   self.assertEqual(memory.conn.execute('SELECT count(*) FROM security_visual_cases').fetchone()[0],6)
   memory.conn.close()
 async def test_diagnostics_distinguish_intentional_person_only_policy_and_offline_camera(self):
  profile=SiteProfile(path=None,roles={'security.visual.test':{'entity_id':'camera.test','presence_entities':['binary_sensor.person'],'frigate_cameras':['test'],'enabled':True}})
  with tempfile.TemporaryDirectory() as d:
   memory=MemoryStore(Path(d)/'db');ensure_security_schema(memory.conn)
   monitor=object.__new__(HomeMonitor);monitor.profile=profile;monitor.memory=memory;monitor.conn=memory.conn
   row=monitor.security_health({'camera.test':{'state':'unavailable'},'binary_sensor.person':{'state':'unavailable'}},0)[0]
   self.assertEqual(row['trigger_policy'],'person_only');self.assertFalse(row['motion_fallback']);self.assertEqual(row['status'],'camera_unavailable')
   memory.conn.close()
