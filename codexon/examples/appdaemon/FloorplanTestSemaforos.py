"""Expose the existing nine rolling counters to the floorplan without recording events."""
import appdaemon.plugins.hass.hassapi as hass

class FloorplanTestSemaforos(hass.Hass):
    def initialize(self):
        self.source = self.args.get('source', 'sensor.camera_security_test_count')
        self.targets = self.args['sensors']
        self.last_values = {}
        self.listen_state(self.source_changed, self.source, attribute='all')
        self.publish(self.get_state(self.source, attribute='all'))
        self.log('Floorplan traffic lights ready: %s counters' % len(self.targets))

    def source_changed(self, entity, attribute, old, new, kwargs):
        self.publish(new)

    def publish(self, state):
        state = state if isinstance(state, dict) else {}
        attrs = state.get('attributes') or {}
        rows = attrs.get('sensor_rows') or []
        rows = {r.get('entity'): r for r in rows if isinstance(r, dict)}
        available = state.get('state') not in {None, 'unknown', 'unavailable'}
        for target in self.targets:
            row = rows.get(target['source_entity'])
            counts = None
            if available and row is not None:
                try:
                    counts = tuple(max(0, int(row.get(k, 0))) for k in ('count_5min', 'count_1h', 'count_24h'))
                except (ValueError, TypeError, OverflowError):
                    pass
            if self.last_values.get(target['entity_id'], object()) == counts:
                continue
            values = tuple(str(n) for n in counts) if counts is not None else ('unavailable',) * 3
            self.set_state(target['entity_id'], state=values[0], replace=True, attributes={
                'friendly_name': 'Semáforo ' + target['name'], 'icon': 'mdi:traffic-light',
                'count_5min': values[0], 'count_1h': values[1], 'count_24h': values[2],
                'source_entity': target['source_entity'], 'source_counter': self.source,
            })
            # Strings preserve zero-valued attributes through AppDaemon's HA bridge.
            self.last_values[target['entity_id']] = counts
