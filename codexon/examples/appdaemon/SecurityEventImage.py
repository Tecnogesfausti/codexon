"""Immutable notification attachments from Frigate person events."""
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def event_image(camera, source, root, now=None, opener=urlopen, base_url="http://frigate:5000"):
    if not camera:
        return None
    now = time.time() if now is None else now
    base = base_url.rstrip('/')
    aliases = camera.get('frigate_names', [])
    if source.startswith('frigate:'):
        _, alias, event_id = source.split(':', 2)
        if alias not in aliases or not re.fullmatch(r'[A-Za-z0-9_.-]+', event_id):
            return None
    else:
        candidates = []
        for alias in aliases:
            query = urlencode({'camera': alias, 'label': 'person', 'after': now-60,
                               'before': now+1, 'include_thumbnails': 0, 'limit': 10})
            with opener(base+'/api/events?'+query, timeout=2) as response:
                events = json.load(response)
            candidates.extend(e for e in events if e.get('camera') == alias
                and e.get('label') == 'person' and e.get('has_snapshot')
                and e.get('false_positive') is not True
                and float(e.get('start_time', 0)) <= now+1
                and (e.get('end_time') is None or float(e['end_time']) >= now-30))
        if not candidates:
            return None
        event_id = max(candidates, key=lambda e: e['start_time'])['id']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', event_id):
            return None
    with opener(base+'/api/events/'+event_id+'/snapshot.jpg', timeout=2) as response:
        data = response.read(5*1024*1024+1)
    if not data.startswith(b'\xff\xd8\xff') or not 100 < len(data) <= 5*1024*1024:
        raise ValueError('Invalid event JPEG')
    directory = Path(root)/'www'/'security_alerts'
    directory.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex+'.jpg'
    target = directory/name
    target.write_bytes(data)
    for old in directory.glob('*.jpg'):
        if re.fullmatch(r'[a-f0-9]{32}\.jpg', old.name) and old.stat().st_mtime < now-7*86400:
            try:old.unlink()
            except OSError:pass
    return {'image': '/local/security_alerts/'+name, 'snapshot': str(target), 'event_id': event_id}
