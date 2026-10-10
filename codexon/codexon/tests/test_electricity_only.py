import json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from codexon import MemoryStore
from site_profile import SiteProfile,SiteProfileError
from services.home_monitor import HomeMonitor
from services.security_visual import camera_configs

class ElectricityOnlyTests(unittest.IsolatedAsyncioTestCase):
 async def test_electricity_only_poll_uses_recorder_without_water_or_camera_calls(self):
  with tempfile.TemporaryDirectory() as d:
   memory=MemoryStore(Path(d)/'db');profile=SiteProfile.load(Path(__file__).resolve().parents[1]/'site.electricity.example.yaml')
   agent=SimpleNamespace(memory=memory,ha_base_url='http://ha.test',ha_token='test-only',httpx=Mock(),site_profile=profile);monitor=HomeMonitor(agent,Mock());self.assertEqual(monitor.timezone,'Europe/Madrid');self.assertEqual(camera_configs(profile),())
   monitor.reader.get_states=AsyncMock(return_value=[{'entity_id':'sensor.main_power','state':'325','attributes':{'unit_of_measurement':'W'}},{'entity_id':'sensor.main_energy_today','state':'2.5','attributes':{'unit_of_measurement':'kWh'}}]);monitor.last_history=-3601
   with patch('services.home_monitor.ha_websocket_command',new_callable=AsyncMock,return_value={'sensor.main_energy':[{'start':time.time()-7200,'change':.2}]}) as recorder:
    await monitor.poll()
   payload=recorder.call_args.args[1];self.assertEqual(payload['type'],'recorder/statistics_during_period');self.assertEqual(payload['statistic_ids'],['sensor.main_energy'])
   snapshot=json.loads(memory.get_setting('home_monitor.snapshot'));self.assertIsNone(snapshot['water']);self.assertEqual(snapshot['security'],[]);self.assertEqual(snapshot['alerts'],[]);self.assertEqual(snapshot['energy']['total_power_w'],325);self.assertEqual(snapshot['energy']['today_kwh'],2.5);self.assertEqual(snapshot['energy']['circuits'][0]['status'],'learning');memory.conn.close()
 async def test_empty_profile_is_valid_without_device_warnings(self):
  with tempfile.TemporaryDirectory() as d:
   memory=MemoryStore(Path(d)/'db');agent=SimpleNamespace(memory=memory,ha_base_url='http://ha.test',ha_token='test-only',httpx=Mock(),site_profile=SiteProfile.empty());monitor=HomeMonitor(agent,Mock());monitor.reader.get_states=AsyncMock(return_value=[])
   with patch('services.home_monitor.ha_websocket_command',new_callable=AsyncMock) as recorder:await monitor.poll()
   recorder.assert_not_awaited();snapshot=json.loads(memory.get_setting('home_monitor.snapshot'));self.assertIsNone(snapshot['water']);self.assertEqual(snapshot['security'],[]);self.assertEqual(snapshot['alerts'],[]);memory.conn.close()
 def test_timezone_validation_and_legacy_water_override(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'site.yaml'
   for bad in ('Invalid/Zone','../UTC',12):
    p.write_text('version: 1\nroles: {}\ntimezone: '+str(bad)+'\n')
    with self.assertRaises(SiteProfileError):SiteProfile.load(p)
   p.write_text('version: 1\nroles: {}\n');self.assertEqual(SiteProfile.load(p).timezone,'UTC')
   memory=MemoryStore(Path(d)/'db');profile=SiteProfile(path=None,roles={'water.monitor':{'entity_id':'sensor.water','timezone':'Europe/Madrid'}});monitor=HomeMonitor(SimpleNamespace(memory=memory,ha_base_url='http://ha.test',ha_token='test-only',httpx=Mock(),site_profile=profile),Mock());self.assertEqual(monitor.timezone,'Europe/Madrid');memory.conn.close()
