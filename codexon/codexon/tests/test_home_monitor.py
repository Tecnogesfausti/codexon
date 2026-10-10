import datetime as dt,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from codexon import MemoryStore
from site_profile import SiteProfile
from services.home_monitor import ensure_monitor_schema,ingest_hours,analyze_energy,analyze_water,observe_snapshot
from services.security_visual import utc_now

class MonitorTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.memory=MemoryStore(Path(self.tmp.name)/'db');self.conn=self.memory.conn;ensure_monitor_schema(self.conn)
 def tearDown(self):self.conn.close();self.tmp.cleanup()
 def profile(self):return SiteProfile(path=None,roles={'energy.circuit.office':{'entity_id':'sensor.power','energy_entity':'sensor.energy','daily_entity':'sensor.daily','label':'Oficina'},'water.monitor':{'entity_id':'sensor.water','max_flow_lpm':60},'irrigation.huerta.switches':{'entities':['switch.zone']},'irrigation.test':{'entity_id':'switch.zone','kind':'zone'}})
 def states(self,liters='100',valve='off'):
  return {'sensor.water':{'state':liters,'attributes':{'unit_of_measurement':'L'}},'switch.zone':{'state':valve},'sensor.power':{'state':'100','attributes':{'unit_of_measurement':'W'}},'sensor.daily':{'state':'2','attributes':{'unit_of_measurement':'kWh'}}}
 def test_history_reset_and_bad_or_partial_bins_do_not_poison_training(self):
  now=1800000000
  result=ingest_hours(self.conn,{'sensor.energy':[{'start':(now-7200)*1000,'change':.2},{'start':now-3600,'change':-20},{'start':now-3600,'change':float('nan')},{'start':now-100,'change':.2},{'start':now-3600,'change':999}]},now)
  self.assertEqual(result,{'accepted':1,'rejected':4})
 def test_learns_context_without_training_on_the_evaluated_hour(self):
  now=dt.datetime(2026,10,4,12,tzinfo=dt.timezone.utc).timestamp()
  rows=[{'start':now-hour*3600,'change':.1 if hour>1 else 1.5} for hour in range(1,672)]
  ingest_hours(self.conn,{'sensor.energy':rows},now)
  result=analyze_energy(self.conn,self.profile(),self.states(),now,'Europe/Madrid')
  c=result['circuits'][0];self.assertEqual(c['status'],'trained');self.assertEqual(c['expected_hour_kwh'],.1)
  self.assertEqual(c['anomalies'][0]['kind'],'hour_excess');self.assertEqual(result['total_power_w'],100)
 def test_unknown_power_is_not_zero(self):
  states=self.states();states['sensor.power']['state']='unavailable'
  result=analyze_energy(self.conn,self.profile(),states,time.time(),'UTC')
  self.assertIsNone(result['total_power_w']);self.assertEqual(result['circuits'][0]['status'],'unavailable')
 def test_closed_relays_leak_needs_repeated_advances(self):
  now=1800000000
  for i in range(11):result=analyze_water(self.conn,self.profile(),self.states(str(100+i)),now+i*60)
  self.assertTrue(any(a['kind']=='flow_when_closed' for a in result['alerts']))
 def test_reset_and_restart_gap_are_not_a_leak(self):
  now=1800000000
  analyze_water(self.conn,self.profile(),self.states('1000'),now)
  result=analyze_water(self.conn,self.profile(),self.states('1'),now+60)
  self.assertTrue(result['counter_reset']);self.assertFalse(any(a['kind']=='flow_when_closed' for a in result['alerts']))
  result=analyze_water(self.conn,self.profile(),self.states('101'),now+1000)
  self.assertGreater(result['gaps'],0);self.assertIsNone(result['flow_lpm_5min'])
 def test_unknown_relay_cannot_prove_all_valves_closed(self):
  now=1800000000
  for i in range(11):r=analyze_water(self.conn,self.profile(),self.states(str(100+10*i),'unavailable'),now+i*60)
  self.assertFalse(r['coverage']);self.assertFalse(any(a['kind']=='flow_when_closed' for a in r['alerts']))
 def test_overlapping_zones_and_sampling_gaps_are_not_saved_as_clean_sessions(self):
  profile=self.profile();now=1800000000
  analyze_water(self.conn,profile,self.states('100','on'),now)
  analyze_water(self.conn,profile,self.states('200','off'),now+1000)
  self.assertEqual(self.conn.execute('SELECT count(*) FROM water_sessions').fetchone()[0],0)

class ObservationTests(unittest.IsolatedAsyncioTestCase):
 async def test_repeated_poll_does_not_call_llm_or_extract_memory(self):
  with tempfile.TemporaryDirectory() as d:
   memory=MemoryStore(Path(d)/'db')
   memory.set_setting('home_monitor.snapshot',json.dumps({'updated_at':utc_now(),'energy':{'circuits':[{}],'total_power_w':50},'alerts':[]}))
   agent=SimpleNamespace(memory=memory)
   await observe_snapshot(agent);await observe_snapshot(agent)
   self.assertEqual(memory.conn.execute("SELECT count(*) FROM observations WHERE source='sensor-loop'").fetchone()[0],1)
   self.assertEqual(json.loads(memory.get_setting('sensor_observer.efficiency'))['llm_calls_avoided'],4);memory.conn.close()

class SmallLeakTests(unittest.TestCase):
 setUp=MonitorTests.setUp
 tearDown=MonitorTests.tearDown
 profile=MonitorTests.profile
 states=MonitorTests.states
 def test_low_resolution_slow_leak_is_seen_over_a_longer_window(self):
  now=1800000000
  for i in range(91):r=analyze_water(self.conn,self.profile(),self.states(str(100+i//15)),now+i*60)
  self.assertTrue(any(a['kind']=='small_closed_leak' for a in r['alerts']))

class EventCoverageTests(unittest.TestCase):
 setUp=MonitorTests.setUp
 tearDown=MonitorTests.tearDown
 profile=MonitorTests.profile
 states=MonitorTests.states
 def test_short_irrigation_between_polls_is_not_misidentified_as_a_closed_valve_leak(self):
  now=1800000000
  for i in range(11):
   self.conn.execute('INSERT INTO water_events VALUES(?,?,?)',(now+i*60-20,'switch.zone','on'))
   self.conn.execute('INSERT INTO water_events VALUES(?,?,?)',(now+i*60-10,'switch.zone','off'))
   r=analyze_water(self.conn,self.profile(),self.states(str(100+i*10)),now+i*60)
  self.assertFalse(any(a['kind']=='flow_when_closed' for a in r['alerts']))
 def test_event_disconnection_invalidates_closed_valve_coverage(self):
  p=self.profile();p.roles['water.monitor']['require_event_tracking']=True
  self.memory.set_setting('home_monitor.events',json.dumps({'connected':False}))
  r=analyze_water(self.conn,p,self.states(),1800000000)
  self.assertFalse(r['coverage'])

class ZoneVolumeTests(unittest.TestCase):
 setUp=MonitorTests.setUp
 tearDown=MonitorTests.tearDown
 profile=MonitorTests.profile
 states=MonitorTests.states
 def test_large_session_volume_is_detected_even_before_the_zone_closes(self):
  now=1800000000
  for i in range(5):self.conn.execute('INSERT INTO water_sessions(zone,started,ended,liters,seconds) VALUES(?,?,?,?,?)',('switch.zone',now-i*3600-600,now-i*3600,100,600))
  for i in range(5):r=analyze_water(self.conn,self.profile(),self.states(str(100+i*75),'on'),now+i*60)
  self.assertTrue(any(a['kind']=='zone_volume_excess' for a in r['alerts']))
