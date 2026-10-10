"""Measured consumption learning and availability checks; no LLM or device control."""
from __future__ import annotations
import asyncio, contextlib, datetime as dt, hashlib, json, math, statistics, time
from zoneinfo import ZoneInfo
from agents.monitor_temperatura import HomeAssistantRestReader
from tools.homeassistant import ha_websocket_command
from services.security_visual import camera_configs, timestamp, utc_now
from services.security_image_storage import sync_controls


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def ensure_monitor_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS consumption_hours(entity_id TEXT NOT NULL,start REAL NOT NULL,kwh REAL NOT NULL,PRIMARY KEY(entity_id,start));
    CREATE TABLE IF NOT EXISTS water_samples(ts REAL PRIMARY KEY,liters REAL,active_json TEXT NOT NULL,coverage INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS water_events(ts REAL NOT NULL,entity_id TEXT NOT NULL,state TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS water_events_ts ON water_events(ts);
    CREATE TABLE IF NOT EXISTS water_sessions(id INTEGER PRIMARY KEY,zone TEXT NOT NULL,started REAL NOT NULL,ended REAL NOT NULL,liters REAL NOT NULL,seconds REAL NOT NULL);
    ''')
    conn.commit()


def circuits(profile):
    return [(key, row) for key, row in profile.roles.items() if key.startswith('energy.circuit.')]


def ingest_hours(conn, data, now):
    accepted = rejected = 0
    for eid, rows in data.items():
        for row in rows:
            start = number(row.get('start'))
            if start is not None and start > 1e11:
                start /= 1000
            value = number(row.get('change'))
            # Recorder change accounts for counter resets; raw subtraction does not.
            if start is None or start + 3600 > now or value is None or value < 0 or value > 100:
                rejected += 1
                continue
            conn.execute('INSERT OR REPLACE INTO consumption_hours VALUES(?,?,?)',(eid,start,value))
            accepted += 1
    conn.commit()
    return {'accepted':accepted,'rejected':rejected}


def learned_threshold(values, absolute_floor):
    median = statistics.median(values)
    mad = statistics.median(abs(v-median) for v in values)
    return median, max(median*2.5,median+6*1.4826*mad,median+absolute_floor)


def analyze_energy(conn, profile, states, now, timezone):
    zone = ZoneInfo(timezone)
    rows_out = []
    alerts = []
    price_row = states.get(profile.entity('energy.price'), {})
    price = number(price_row.get('state')) if price_row.get('attributes',{}).get('unit_of_measurement') in {'€/kWh','EUR/kWh'} else None
    for role, cfg in circuits(profile):
        energy_id = cfg.get('energy_entity')
        history = [(r[0],r[1]) for r in conn.execute('SELECT start,kwh FROM consumption_hours WHERE entity_id=? AND start>=? ORDER BY start',(energy_id,now-35*86400))]
        power_row = states.get(cfg.get('entity_id'),{})
        power = number(power_row.get('state')) if power_row.get('attributes',{}).get('unit_of_measurement') == 'W' else None
        day_row = states.get(cfg.get('daily_entity'),{})
        today = number(day_row.get('state')) if day_row.get('attributes',{}).get('unit_of_measurement') == 'kWh' else None
        item = {'name':cfg.get('label',role),'entity_id':cfg['entity_id'],'energy_entity':energy_id,'power_w':power,'today_kwh':today,'hours':len(history),'status':'learning','hour_profile':[],'weekday_profile':[],'anomalies':[]}
        training = [(dt.datetime.fromtimestamp(t,zone),v) for t,v in history if t < now-86400]
        for hour in range(24):
            peers = [v for d,v in training if d.hour==hour]
            if peers:
                med, high = learned_threshold(peers,.15)
                item['hour_profile'].append({'hour':hour,'median_kwh':round(med,4),'high_kwh':round(high,4),'samples':len(peers)})
        days = {}
        for t,v in history:
            d = dt.datetime.fromtimestamp(t,zone)
            days.setdefault(d.date(),[]).append((d,v))
        complete_days = {d:sum(v for _,v in vs) for d,vs in days.items() if len(vs)>=23 and d < dt.datetime.fromtimestamp(now,zone).date()}
        for weekday in range(7):
            peers = [v for d,v in complete_days.items() if d.weekday()==weekday and d < dt.datetime.fromtimestamp(now-86400,zone).date()]
            if peers:
                med,high=learned_threshold(peers,.8)
                item['weekday_profile'].append({'weekday':weekday,'median_kwh':round(med,3),'high_kwh':round(high,3),'samples':len(peers)})
        if history and now-history[-1][0] < 3*3600:
            t,value=history[-1];local=dt.datetime.fromtimestamp(t,zone)
            peers=[v for d,v in training if d.hour==local.hour and (d.weekday()<5)==(local.weekday()<5)]
            if len(peers)>=5:
                med,high=learned_threshold(peers,.15)
                item.update(status='trained',expected_hour_kwh=round(med,4),last_hour_kwh=round(value,4),baseline_samples=len(peers))
                if value>high:
                    item['anomalies'].append({'kind':'hour_excess','message':f"{item['name']}: {value:.3f} kWh en la última hora completa frente a {med:.3f} habituales.",'excess_kwh':round(value-med,3),'threshold_kwh':round(high,3)})
            # Compare full days against the same weekday, excluding the evaluated day.
            if complete_days:
                d=max(complete_days);value=complete_days[d]
                peers=[v for day,v in complete_days.items() if day<d and day.weekday()==d.weekday()]
                if len(peers)>=3:
                    med,high=learned_threshold(peers,.8)
                    if value>high:
                        item['anomalies'].append({'kind':'day_excess','message':f"{item['name']}: {value:.2f} kWh el {d}, frente a {med:.2f} habituales ese día de semana.",'excess_kwh':round(value-med,3)})
        elif history:
            item['status']='history_stale'
        if power is None:
            item['status']='unavailable'
        night=[v for d,v in training if 1<=d.hour<6]
        if len(night)>=15:
            base=statistics.median(night)
            item['night_base_w']=round(base*1000,1)
            # A scenario, never an asserted saving or a recommendation to cut refrigerators.
            item['saving_scenario']={'reduction_percent':20,'hours_per_day':5,'kwh_30_days':round(base*.2*5*30,3),'eur_30_days_at_current_price':round(base*.2*5*30*price,2) if price is not None else None,'message':'Si se redujera un 20% la base nocturna durante 5 h/día. Identificar primero las cargas que pueden apagarse.'}
        alerts.extend({'severity':'warning',**a,'entity_id':cfg['entity_id']} for a in item['anomalies'])
        rows_out.append(item)
    return {'circuits':rows_out,'alerts':alerts,'price_eur_kwh':price,'total_power_w':round(sum(r['power_w'] for r in rows_out),1) if rows_out and all(r['power_w'] is not None for r in rows_out) else None,'today_kwh':round(sum(r['today_kwh'] for r in rows_out),3) if rows_out and all(r['today_kwh'] is not None for r in rows_out) else None,'coverage':'Suma de los cinco circuitos configurados; no se añade suma_pias ni se atribuye todo el circuito a un solo aparato.'}


def analyze_water(conn, profile, states, now):
    cfg=profile.binding('water.monitor') or {}
    eid=cfg.get('entity_id')
    meter=states.get(eid,{})
    value=number(meter.get('state')) if meter.get('attributes',{}).get('unit_of_measurement')=='L' else None
    switches=profile.entities('irrigation.huerta.switches')
    valid=bool(switches) and all(states.get(e,{}).get('state') in {'on','off'} for e in switches) and all(states.get(e,{}).get('state')=='on' for e in cfg.get('availability_entities',[]))
    events_row=conn.execute("SELECT value FROM settings WHERE key='home_monitor.events'").fetchone()
    if cfg.get('require_event_tracking'):
        valid = valid and bool(events_row and json.loads(events_row[0]).get('connected'))
    active=[e for e in switches if states.get(e,{}).get('state')=='on']
    zone_switches=set(e for role,b in profile.roles.items() if role.startswith('irrigation.') and b.get('kind')=='zone' for e in [b.get('entity_id')] if e)
    zones=[e for e in active if e in zone_switches]
    calibration=number(states.get(cfg.get('calibration_entity'),{}).get('state'))
    resolution=1/calibration if calibration and calibration>0 else None
    previous=conn.execute('SELECT * FROM water_samples ORDER BY ts DESC LIMIT 1').fetchone()
    conn.execute('INSERT OR IGNORE INTO water_samples VALUES(?,?,?,?)',(now,value,json.dumps(active),int(valid)))
    conn.commit()
    recent=[dict(r) for r in conn.execute('SELECT * FROM water_samples WHERE ts>=? ORDER BY ts',(now-1800,))]
    alerts=[];gaps=0;reset=False;deltas=[]
    for a,b in zip(recent,recent[1:]):
        seconds=b['ts']-a['ts']
        if seconds<=0 or seconds>150 or a['liters'] is None or b['liters'] is None:
            gaps+=1;continue
        delta=b['liters']-a['liters']
        if delta<0:
            reset=True;continue
        deltas.append((b['ts'],delta,seconds, bool(a['coverage'] and b['coverage'] and json.loads(a['active_json'])==[] and json.loads(b['active_json'])==[] and not conn.execute('SELECT 1 FROM water_events WHERE ts>? AND ts<=? LIMIT 1',(a['ts'],b['ts'])).fetchone())))
    recent5=[x for x in deltas if x[0]>=now-300]
    liters5=sum(x[1] for x in recent5)
    seconds5=sum(x[2] for x in recent5)
    flow=liters5/(seconds5/60) if seconds5>=120 else None
    unplanned=[x for x in deltas if x[0]>=now-600 and x[3]]
    unplanned_liters=sum(x[1] for x in unplanned)
    positives=sum(x[1]>0 for x in unplanned)
    if valid and not active and positives>=3 and unplanned_liters>=max(3*(resolution or 1),float(cfg.get('idle_liters_threshold',5))):
        alerts.append({'severity':'critical' if unplanned_liters>=50 else 'warning','kind':'flow_when_closed','message':f"{unplanned_liters:.1f} L observados en 10 minutos con todos los relés de riego apagados. Revisar paso de agua o fuga; el estado eléctrico no prueba el cierre físico.",'entity_id':eid})
    # Small persistent advances need at least 20 minutes of uninterrupted measured coverage.
    if valid and not active and len(deltas)>=20 and gaps==0 and not reset and sum(x[1]>0 for x in deltas)>=10 and sum(x[1] for x in deltas)>max(5*(resolution or 1),5):
        alerts.append({'severity':'warning','kind':'persistent_flow','message':'El contador avanza de forma persistente durante al menos 20 minutos. Contrastar con la duración prevista del riego.','entity_id':eid})
    long_rows=[dict(r) for r in conn.execute('SELECT * FROM water_samples WHERE ts>=? ORDER BY ts',(now-3*3600,))]
    closed=[]
    for a,b in zip(long_rows,long_rows[1:]):
        if not a['coverage'] or not b['coverage'] or json.loads(a['active_json']) or json.loads(b['active_json']) or a['liters'] is None or b['liters'] is None or b['ts']-a['ts']>150 or b['liters']<a['liters'] or conn.execute('SELECT 1 FROM water_events WHERE ts>? AND ts<=? LIMIT 1',(a['ts'],b['ts'])).fetchone():
            closed=[]
            continue
        closed.append((b['ts'],b['liters']-a['liters'],b['ts']-a['ts']))
    if closed and sum(x[2] for x in closed)>=3600 and sum(x[1]>0 for x in closed)>=4 and sum(x[1] for x in closed)>=max(4*(resolution or 1),4):
        liters=sum(x[1] for x in closed);minutes=sum(x[2] for x in closed)/60
        alerts.append({'severity':'warning','kind':'small_closed_leak','message':f'Posible fuga pequeña: {liters:.1f} L en {minutes:.0f} minutos con todos los relés apagados, observada en al menos cuatro avances del contador.','entity_id':eid})
    if flow is not None and flow>float(cfg.get('max_flow_lpm',60)):
        alerts.append({'severity':'critical','kind':'high_flow','message':f'Caudal medio observado {flow:.1f} L/min por encima del umbral configurado.','entity_id':eid})
    if value is None or not valid:
        alerts.append({'severity':'warning','kind':'coverage_missing','message':'Vigilancia de agua incompleta: contador o relés sin datos válidos.','entity_id':eid})
    # Session state persists across service restarts; discard gaps/resets/overlapping zones.
    row=conn.execute("SELECT value FROM settings WHERE key='home_monitor.water_session'").fetchone()
    session=json.loads(row[0]) if row else None
    single=zones[0] if len(zones)==1 and all(e in zone_switches or e==profile.entity('irrigation.primary.master') for e in active) else None
    interrupted=bool(session and previous and conn.execute('SELECT 1 FROM water_events WHERE ts>? AND ts<=? AND entity_id NOT IN (?,?) LIMIT 1',(previous['ts'],now,session['zone'],profile.entity('irrigation.primary.master') or '')).fetchone())
    if session and (interrupted or single!=session['zone'] or not valid or value is None or not previous or now-previous['ts']>150 or value<session['last_value']):
        if not interrupted and value is not None and previous and now-previous['ts']<=150 and previous['coverage'] and valid and value>=session['last_value'] and not active and single is None:
            duration=now-session['started'];liters=value-session['start_value']
            if duration>=120 and liters>0:
                conn.execute('INSERT INTO water_sessions(zone,started,ended,liters,seconds) VALUES(?,?,?,?,?)',(session['zone'],session['started'],now,liters,duration))
        session=None
    if single and valid and value is not None:
        if not session:session={'zone':single,'started':now,'start_value':value,'last_value':value}
        session['last_value']=value
        peers=[r[0] for r in conn.execute('SELECT liters/seconds*60 FROM water_sessions WHERE zone=? ORDER BY id DESC LIMIT 30',(single,))]
        if len(peers)>=5 and now-session['started']>=180:
            current=(value-session['start_value'])/(now-session['started'])*60
            med,high=learned_threshold(peers,5)
            volumes=[r[0] for r in conn.execute('SELECT liters FROM water_sessions WHERE zone=? ORDER BY id DESC LIMIT 30',(single,))]
            median_volume,high_volume=learned_threshold(volumes,20)
            used=value-session['start_value']
            if used>high_volume:alerts.append({'severity':'warning','kind':'zone_volume_excess','message':f'{single}: {used:.1f} L en la sesión actual frente a {median_volume:.1f} L habituales; contrastar con el volumen previsto del riego.','entity_id':single})
            if current>high:alerts.append({'severity':'warning','kind':'zone_excess','message':f'{single}: {current:.1f} L/min frente a {med:.1f} habituales en {len(peers)} sesiones medidas.','entity_id':single})
    conn.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES('home_monitor.water_session',?,?)",(json.dumps(session),utc_now()));conn.commit()
    return {'entity_id':eid,'coverage':valid and value is not None,'counter_liters':value,'flow_lpm_5min':round(flow,3) if flow is not None else None,'observed_liters_10min_closed':round(unplanned_liters,3),'active_relays':active,'resolution_liters':round(resolution,4) if resolution else None,'sample_count':len(recent),'counter_reset':reset,'gaps':gaps,'alerts':alerts,'zone_baselines':[dict(r) for r in conn.execute('SELECT zone,count(*) AS samples,avg(liters) AS average_liters,avg(liters/seconds*60) AS average_lpm FROM water_sessions GROUP BY zone')],'note':'Contador acumulado real de riego; no el contador temporal reiniciable. Las fugas inferiores a su resolución necesitan más tiempo para observarse.'}


class HomeMonitor:
    def __init__(self, agent, log):
        self.agent=agent;self.memory=agent.memory;self.conn=self.memory.conn;self.profile=agent.site_profile;self.log=log
        self.timezone=(self.profile.binding('water.monitor') or {}).get('timezone',self.profile.timezone);self.last_history=0.;self.reader=HomeAssistantRestReader(agent)
        ensure_monitor_schema(self.conn)

    def connection_status(self, connected):
        now=time.time()
        self.conn.execute('INSERT INTO water_events VALUES(?,?,?)',(now,'__connection__','connected' if connected else 'disconnected'))
        self.conn.commit()
        self.memory.set_setting('home_monitor.events',json.dumps({'connected':connected,'at':utc_now()}))

    def offer(self, event):
        if event.get('event_type')!='state_changed':return
        data=event.get('data') or {};eid=data.get('entity_id')
        if eid not in self.profile.entities('irrigation.huerta.switches'):return
        new=data.get('new_state') or {};old=data.get('old_state') or {}
        if new.get('state')==old.get('state'):return
        at=timestamp(event.get('time_fired'))
        if at is None or abs(time.time()-at)>150:return
        self.conn.execute('INSERT INTO water_events VALUES(?,?,?)',(at,eid,str(new.get('state'))))
        self.conn.commit()

    async def refresh_history(self, now):
        ids=[r.get('energy_entity') for _,r in circuits(self.profile) if r.get('energy_entity')]
        if not ids:return
        result=await asyncio.wait_for(ha_websocket_command(self.agent,{'type':'recorder/statistics_during_period','start_time':dt.datetime.fromtimestamp(now-28*86400,dt.UTC).isoformat(),'end_time':dt.datetime.fromtimestamp(now,dt.UTC).isoformat(),'statistic_ids':ids,'period':'hour','types':['change']}),30)
        ingestion=ingest_hours(self.conn,result,now)
        self.memory.set_setting('home_monitor.history',json.dumps({'updated_at':utc_now(),**ingestion}))
        self.memory.set_setting('home_monitor.history_error',None)
        self.last_history=time.monotonic()

    def security_health(self, states, now):
        sync_controls(self.conn,states)
        rows=[]
        for c in camera_configs(self.profile):
            detectors=[{'entity_id':e,'state':states.get(e,{}).get('state','missing')} for e in c.presence_entities]
            motion=[{'entity_id':e,'state':states.get(e,{}).get('state','missing')} for e in c.motion_entities]
            self.memory.set_setting('security_visual.native_states.'+c.name,json.dumps({r['entity_id']:r['state'] for r in detectors}))
            native=any(r['state'] in {'on','off'} for r in detectors)
            fallback=not native and any(r['state'] in {'on','off'} for r in motion)
            camera_state=states.get(c.entity_id,{}).get('state','missing')
            last=self.conn.execute('SELECT reviewed_at,result_json FROM security_visual_cases WHERE camera=? ORDER BY id DESC LIMIT 1',(c.name,)).fetchone()
            error=self.conn.execute('SELECT error_code FROM security_visual_reviews r JOIN security_visual_cases c ON c.id=r.case_id WHERE c.camera=? ORDER BY r.id DESC LIMIT 1',(c.name,)).fetchone()
            rows.append({'last_review_at':last[0] if last else None,'last_verdict':(json.loads(last[1] or '{}').get('visual_verdict') if last else None),'last_error':error[0] if error else None,'name':c.name,'camera_state':camera_state,'detectors':detectors,'motion':motion,'native_person_available':native,'motion_fallback':fallback,'trigger_policy':'person_only' if not c.motion_entities else 'person_with_motion_fallback','visual_cooldown_seconds':c.cooldown_seconds,'daily_call_limit':c.daily_call_limit,'frigate_expected':bool(c.frigate_cameras),'frigate_cameras':list(c.frigate_cameras),'frigate_bridge':json.loads(self.memory.get_setting('security_visual.frigate_health.'+c.name,'{}')),'mqtt_state':states.get(c.frigate_event_sensor,{}).get('state','missing'),'status':'camera_unavailable' if camera_state in {'missing','unknown','unavailable'} else 'person_detector_unavailable' if not native else 'ready'})
        return rows

    async def poll(self):
        now=time.time();raw=await self.reader.get_states();states={r['entity_id']:r for r in raw}
        if circuits(self.profile) and time.monotonic()-self.last_history>=3600:
            try:await self.refresh_history(now)
            except Exception as exc:
                self.memory.set_setting('home_monitor.history_error',json.dumps({'at':utc_now(),'code':type(exc).__name__}))
                self.last_history=time.monotonic()-3300
        self.memory.set_setting('home_monitor.time_zone',self.timezone)
        self.memory.set_setting('home_monitor.light_context', 'night' if states.get('sun.sun',{}).get('state')=='below_horizon' else 'day' if states.get('sun.sun',{}).get('state')=='above_horizon' else 'unknown')
        energy=analyze_energy(self.conn,self.profile,states,now,self.timezone)
        water=analyze_water(self.conn,self.profile,states,now) if self.profile.binding('water.monitor') else None
        security=self.security_health(states,now)
        self.memory.set_setting('security_visual.diagnostics',json.dumps(security))
        alerts=energy['alerts']+(water['alerts'] if water else [])
        for r in security:
            bridge=r['frigate_bridge']
            if r['frigate_expected'] and (not bridge.get('reachable') or not any((v or 0)>0 for v in bridge.get('camera_fps',{}).values())):
                alerts.append({'severity':'warning','kind':'frigate_coverage_missing','message':r['name']+': no hay conexión de Frigate y vídeo confirmados; revisar cobertura visual.','entity_id':r['name']})
            if r['status']!='ready':alerts.append({'severity':'warning','kind':r['status'],'message':r['name']+(': cámara Tapo no disponible; consultar cobertura independiente de Frigate.' if r['status']=='camera_unavailable' else ': detector de persona Tapo no disponible; '+('verificación visual por movimiento activa.' if r['motion_fallback'] else 'consultar cobertura de personas de Frigate.')),'entity_id':r['name']})
        snapshot={'updated_at':utc_now(),'energy':energy,'water':water,'security':security,'alerts':alerts,'poll_seconds':60}
        self.memory.set_setting('home_monitor.snapshot',json.dumps(snapshot,ensure_ascii=False))
        self.memory.set_setting('home_monitor.error',None)
        # Deduplicate meaningful transitions, not noisy power values or changing descriptions.
        signature=json.dumps(sorted((a['kind'],a['entity_id'],a['severity']) for a in alerts))
        if self.memory.get_setting('home_monitor.alert_signature')!=signature:
            self.memory.add_observation(source='home_monitor',summary=' · '.join(a['message'] for a in alerts) or 'Supervisión de consumos y seguridad sin anomalías medidas.',raw=json.dumps(snapshot,ensure_ascii=False))
            self.memory.set_setting('home_monitor.alert_signature',signature)
        self.conn.execute('DELETE FROM water_events WHERE ts<?',(now-2*86400,))
        self.conn.execute('DELETE FROM water_samples WHERE ts<?',(now-2*86400,));self.conn.execute('DELETE FROM consumption_hours WHERE start<?',(now-90*86400,));self.conn.commit()
        return snapshot

    async def run(self,stop):
        while not stop.is_set():
            try:await self.poll()
            except asyncio.CancelledError:raise
            except Exception as exc:self.memory.set_setting('home_monitor.error',json.dumps({'at':utc_now(),'code':type(exc).__name__}))
            with contextlib.suppress(asyncio.TimeoutError):await asyncio.wait_for(stop.wait(),60)


async def observe_snapshot(agent):
    """Batch observations from existing workers; never classify/extract memory every 5 min."""
    memory=agent.memory
    raw=memory.get_setting('home_monitor.snapshot','{}')
    snapshot=json.loads(raw)
    if not snapshot.get('updated_at') or time.time()-(timestamp(snapshot['updated_at']) or 0)>180:
        memory.set_setting('sensor_observer.health',json.dumps({'state':'waiting_for_fresh_monitor','at':utc_now()}))
        return
    day=utc_now()[:10]
    stats=json.loads(memory.get_setting('sensor_observer.efficiency','{}'))
    if stats.get('day')!=day:stats={'day':day,'polls':0,'llm_calls_avoided':0,'summaries':0}
    stats['polls']+=1;stats['llm_calls_avoided']+=2
    signature=json.dumps(sorted((a['kind'],a['entity_id'],a['severity']) for a in snapshot['alerts']))
    previous=memory.get_setting('sensor_observer.signature','')
    last=timestamp(memory.get_setting('sensor_observer.last_summary','')) or 0
    # One grouped local report per six hours, and on changes of measured risks.
    if signature!=previous or time.time()-last>=21600:
        energy=snapshot['energy'];water=snapshot.get('water')
        report=f"Supervisión agrupada: {len(energy['circuits'])} circuitos; potencia total {energy['total_power_w']} W; {len(snapshot['alerts'])} incidencias."+(f" Agua: {water['flow_lpm_5min']} L/min observados." if water else '')
        memory.add_observation(source='sensor-loop',summary=report,raw=raw)
        memory.set_setting('sensor_observer.signature',signature);memory.set_setting('sensor_observer.last_summary',utc_now());stats['summaries']+=1
    memory.set_setting('sensor_observer.efficiency',json.dumps(stats))
    memory.set_setting('sensor_observer.health',json.dumps({'state':'active','at':utc_now(),'strategy':'deterministic_grouped_observations'}))
