"""Count real Tapo/Frigate detections; this app never controls alarm devices."""
import json
import math
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import urlopen

import appdaemon.plugins.hass.hassapi as hass


def timestamp(value):
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


def merge_times(tapo_times, events, now, tolerance=10):
    """Frigate track IDs count once; overlapping Tapo activations are duplicates."""
    tracks = [e for e in events if now-86400 <= e['start_time'] <= now]
    times = [e['start_time'] for e in tracks]
    intervals = [(e['start_time']-tolerance,
                  (e.get('end_time') or e.get('seen_at', e['start_time']))+tolerance)
                 for e in tracks]
    for seen in tapo_times:
        if now-86400 <= seen <= now and not any(start <= seen <= end for start, end in intervals):
            times.append(seen)
    return sorted(times)


class SecurityPresenceCounts(hass.Hass):
    def initialize(self):
        self.source = self.args.get('source', 'sensor.camera_security_test_count')
        self.output = self.args.get('output', 'sensor.camera_security_presence_count')
        self.targets = self.args['sensors']
        self.aliases = {}
        self.camera_entities = set()
        for camera in self.args['cameras']:
            # Each camera currently has exactly one native person sensor.
            assert len(camera['person_sensors']) == 1
            entity = camera['person_sensors'][0]
            self.camera_entities.add(entity)
            for alias in camera['frigate_names']:
                assert alias not in self.aliases
                self.aliases[alias] = entity
        self.tapo = {entity: set() for entity in self.camera_entities}
        self.events = {}
        self.history_ready = False
        self.frigate_ready = False
        self.frigate_health = 'initializing'
        self.last_payload = None
        self.last_error = None
        for entity in self.camera_entities:
            self.listen_state(self.tapo_changed, entity, attribute='all')
        self.listen_state(self.source_changed, self.source, attribute='all')
        self.listen_state(self.frigate_changed, 'sensor.frigate_event', attribute='all')
        self.run_in(self.bootstrap_history, 1)
        self.run_every(self.poll_frigate, 'now+2', 10)
        self.publish()

    def bootstrap_history(self, kwargs):
        try:
            histories = self.get_history(entity_id=sorted(self.camera_entities),
                start_time=datetime.now(timezone.utc)-timedelta(days=1),
                no_attributes=True, significant_changes_only=True)
            if not isinstance(histories, list):
                raise ValueError('History response missing')
            for history in histories:
                if not history:
                    continue
                entity = history[0].get('entity_id')
                if entity not in self.tapo:
                    continue
                previous = history[0].get('state')
                # HA inserts an initial state at the query boundary, not an activation.
                for state in history[1:]:
                    if state.get('state') == 'on' and previous != 'on':
                        seen = timestamp(state.get('last_changed'))
                        if seen is not None:
                            self.tapo[entity].add(seen)
                    previous = state.get('state')
            self.history_ready = True
            self.publish()
        except Exception as exc:
            self.log('Presence counter history not ready: '+type(exc).__name__, level='WARNING')
            self.run_in(self.bootstrap_history, 30)

    def tapo_changed(self, entity, attribute, old, new, kwargs):
        if isinstance(new, dict) and new.get('state') == 'on' and (old or {}).get('state') != 'on':
            seen = timestamp(new.get('last_changed'))
            self.tapo[entity].add(seen if seen is not None else time.time())
            self.publish()

    def source_changed(self, entity, attribute, old, new, kwargs):
        self.publish()

    def add_event(self, event, now):
        if not isinstance(event, dict) or event.get('label') != 'person' or event.get('false_positive') is True:
            return
        entity = self.aliases.get(event.get('camera'))
        start = timestamp(event.get('start_time'))
        event_id = event.get('id')
        if entity is None or not event_id or start is None or not now-86400 <= start <= now:
            return
        end = timestamp(event.get('end_time'))
        existing = self.events.get(event_id)
        if existing and existing['entity'] != entity:
            return
        if existing and end is None:
            end = existing.get('end_time')
        self.events[event_id] = {'entity':entity, 'start_time':start, 'end_time':end, 'seen_at':now}

    def frigate_changed(self, entity, attribute, old, new, kwargs):
        attrs = (new or {}).get('attributes') or {}
        self.add_event(attrs.get('after'), time.time())
        self.publish()

    def fetch_events(self, now):
        params = urlencode({'label':'person','include_thumbnails':0,'limit':1000,'after':now-86400})
        with urlopen(self.args['frigate_url'].rstrip('/')+'/api/events?'+params, timeout=5) as response:
            events = json.load(response)
        if not isinstance(events, list) or len(events) >= 1000:
            raise ValueError('Incomplete Frigate event response')
        return events

    def poll_frigate(self, kwargs):
        now = time.time()
        try:
            for event in self.fetch_events(now):
                self.add_event(event, now)
            self.frigate_ready = True
            self.frigate_health = 'ok'
            self.last_error = None
        except Exception as exc:
            self.frigate_health = 'unavailable'
            if self.last_error != type(exc).__name__:
                self.log('Presence counters: Frigate polling failed: '+type(exc).__name__, level='WARNING')
                self.last_error = type(exc).__name__
        self.events = {key:e for key,e in self.events.items() if e['start_time'] >= now-86400}
        self.tapo = {entity:{seen for seen in values if seen >= now-86400} for entity,values in self.tapo.items()}
        self.publish()

    def publish(self):
        if not (self.history_ready and self.frigate_ready):
            self.last_payload = None
            self.set_state(self.output, state='unavailable', replace=True,
                attributes={'friendly_name':'Presencias Tapo y Frigate','status':'initializing'})
            return
        now = time.time()
        raw = self.get_state(self.source, attribute='all') or {}
        raw_attrs = raw.get('attributes') or {}
        raw_rows = {row['entity']:row for row in raw_attrs.get('sensor_rows',[])}
        rows = []
        for target in self.targets:
            entity = target['source_entity']
            if entity in self.camera_entities:
                events = [e for e in self.events.values() if e['entity'] == entity]
                times = merge_times(self.tapo[entity], events, now)
                counts = [sum(seen >= now-seconds for seen in times) for seconds in (300,3600,86400)]
            else:
                row = raw_rows.get(entity)
                if row is None or raw.get('state') in ('unknown','unavailable',None):
                    self.last_payload = None
                    self.set_state(self.output, state='unavailable', replace=True,
                        attributes={'friendly_name':'Presencias Tapo y Frigate','status':'physical_source_unavailable'})
                    return
                counts = [int(row.get(key,0)) for key in ('count_5min','count_1h','count_24h')]
            rows.append({'entity':entity,'name':target['name'],
                **dict(zip(('count_5min','count_1h','count_24h'),counts))})
        totals = {key:sum(row[key] for row in rows) for key in ('count_5min','count_1h','count_24h')}
        table = '\n'.join(['| nombre sensor | 5min | 1h | 24h |','|---|---:|---:|---:|',
            *[f"| {row['name']} | {row['count_5min']} | {row['count_1h']} | {row['count_24h']} |" for row in rows]])
        attrs = {'friendly_name':'Presencias Tapo y Frigate','icon':'mdi:shield-check',
            'sensor_rows':rows,'sensor_table':table,'total_5min':totals['count_5min'],
            'total_1h':totals['count_1h'],'total_24h':totals['count_24h'],
            'sources':['tapo','frigate','physical_sensors'], 'frigate_status':self.frigate_health,
            'tapo_history_24h_counts':{entity:len(values) for entity,values in self.tapo.items()},
            'deduplication_seconds':10,'available_count':len(rows),
            'test_active':raw_attrs.get('test_active',False),
            'active_sensors':raw_attrs.get('active_sensors',[]),
            'active_sensor_names':raw_attrs.get('active_sensor_names',[])}
        payload=(str(totals['count_5min']),attrs)
        if payload != self.last_payload:
            self.set_state(self.output,state=payload[0],replace=True,attributes=attrs)
            self.last_payload=payload
