import hmac
import json
import math
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from ajustes import CONFIG, DB, ROOT, SITE, PEER
from escucha import peer
METRIC = 'outdoor_temperature'
ENTITY = CONFIG['temperature_entity']
BASE = '/intercambio/v1'
BASE2 = '/intercambio/v2'

UTC = timezone.utc

def stamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError()
    return dt.astimezone(UTC)

def fetch(path):
    request = Request('http://supervisor/core/api' + path, headers={
        'Authorization': 'Bearer ' + os.environ['SUPERVISOR_TOKEN']})
    with urlopen(request, timeout=15) as response:
        return json.load(response)

def sample(row, historical=False):
    at = row.get('last_updated') or row['last_changed']
    value = None
    try:
        n = float(row['state'])
        if math.isfinite(n) and row['attributes'].get('unit_of_measurement') == '°C':
            value = n
    except (TypeError, ValueError):
        pass
    quality = 'unavailable' if value is None else 'ok'
    if not historical and value is not None and datetime.now(UTC)-stamp(at) > timedelta(minutes=15):
        quality = 'stale'
    return dict(site=SITE, metric=METRIC, observed_at=at, value=value, unit='°C', quality=quality)

class API(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, obj):
        payload = json.dumps(obj, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def local_client(self):
        return hmac.compare_digest(self.headers.get('Authorization','').encode(), self.server.local_auth)

    def authorized(self):
        return self.local_client() or hmac.compare_digest(self.headers.get('Authorization','').encode(), self.server.auth)

    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {'error':'unauthorized'})
        uri = urlsplit(self.path)
        try:
            if uri.path == BASE2+'/manifest':
                data = dict(version=2, site=SITE, timezone=CONFIG['timezone'], operations=['latest','history','messages'], metrics=[dict(id=METRIC, unit='°C', kind='gauge', max_age_seconds=900)])
            elif uri.path == BASE2+'/messages':
                q = parse_qs(uri.query, strict_parsing=True)
                after = q.get('after', [None])[0]
                table = 'messages' if self.local_client() else 'outbox'
                sender,recipient = (PEER,SITE) if self.local_client() else (SITE,PEER)
                limit = int(q.get('limit',['100'])[0])
                if not 1 <= limit <= 100:
                    return self.reply(400, {'error':'invalid_limit'})
                with sqlite3.connect(DB) as db:
                    cursor = 0
                    if after:
                        row = db.execute(f'SELECT rowid FROM {table} WHERE id=?',(str(uuid.UUID(after)),)).fetchone()
                        if not row:
                            return self.reply(400, {'error':'unknown_cursor'})
                        cursor = row[0]
                    rows = db.execute(f'SELECT id,thread_id,body,created_at FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?', (cursor,limit)).fetchall()
                data = {'messages':[dict(zip(('id','thread_id','body','created_at'),r),sender=sender,recipient=recipient) for r in rows]}
            elif uri.path == BASE+'/manifest':
                data = dict(version=1, site=SITE, timezone=CONFIG['timezone'], operations=['latest','history'],
                            metrics=[dict(id=METRIC, unit='°C', kind='gauge', max_age_seconds=900)])
            elif uri.path == BASE+'/latest/'+METRIC:
                data = sample(fetch('/states/'+ENTITY))
            elif uri.path == BASE+'/history/'+METRIC:
                try:
                    q = parse_qs(uri.query, strict_parsing=True)
                    if set(q) != {'start','end'} or any(len(v)!=1 for v in q.values()):
                        raise ValueError()
                    start,end = (stamp(q[k][0]) for k in ('start','end'))
                    if not timedelta(0) < end-start <= timedelta(days=1):
                        raise ValueError()
                except (ValueError, OverflowError):
                    return self.reply(400, {'error':'invalid_interval'})
                rows = fetch('/history/period/'+start.isoformat()+'?'+urlencode({
                    'filter_entity_id':ENTITY, 'end_time':end.isoformat(), 'skip_initial_state':'1'}))
                samples = [sample(row, True) for group in rows for row in group
                           if start <= stamp(row.get('last_updated') or row['last_changed']) < end]
                if len(samples)>2000:
                    return self.reply(422, {'error':'subdivide_interval'})
                data = {'samples':samples}
            else:
                return self.reply(404, {'error':'not_found'})
        except Exception:
            return self.reply(503, {'error':'source_unavailable'})
        self.reply(200, data)

    def do_POST(self):
        if not self.authorized():
            return self.reply(401, {'error':'unauthorized'})
        if urlsplit(self.path).path != BASE2+'/messages':
            return self.reply(404, {'error':'not_found'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 65536: raise ValueError()
            obj=json.loads(self.rfile.read(size))
            mid=str(uuid.UUID(obj['id'])); thread=str(uuid.UUID(obj['thread_id']))
            body=obj['body']
            if not isinstance(body,str) or not body or len(body)>4096: raise ValueError()
        except Exception:
            return self.reply(400, {'error':'invalid_message'})
        table = 'outbox' if self.local_client() else 'messages'
        created=datetime.now(UTC).isoformat()
        try:
            with sqlite3.connect(DB) as db:
                db.execute('BEGIN IMMEDIATE')
                prior=db.execute(f'SELECT thread_id,body FROM {table} WHERE id=?',(mid,)).fetchone()
                if prior and prior != (thread,body): return self.reply(409, {'error':'id_conflict'})
                db.execute(f'INSERT OR IGNORE INTO {table} VALUES (?,?,?,?)',(mid,thread,body,created))
        except sqlite3.Error:
            return self.reply(503, {'error':'mailbox_unavailable'})
        if self.local_client():
            try:
                ack = peer('POST','',dict(id=mid,thread_id=thread,body=body))
                if ack.get('id') != mid:
                    raise ValueError()
            except Exception:
                return self.reply(202, {'id':mid,'queued':True})
        self.reply(200 if prior else 201, {'id':mid,'thread_id':thread,'accepted':True})

    def denied(self):
        self.reply(405, {'error':'method_not_allowed'})

    do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = denied

if __name__ == '__main__':
    path = (ROOT / 'token_entrante')
    if not path.exists():
        with os.fdopen(os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600),'w') as f:
            f.write(secrets.token_urlsafe(48)+'\n')
    key = path.read_text().strip()
    if len(key) < 32:
        raise RuntimeError('Credencial entrante inválida')
    with sqlite3.connect(DB) as db:
        db.execute('CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)')
    from threading import Thread
    from escucha import listen
    Thread(target=listen, args=(lambda: sample(fetch("/states/"+ENTITY)),), daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0',18741),API)
    local_key = (ROOT/'token_appdaemon').read_text().strip()
    if len(local_key)<32 or local_key==key: raise ValueError('Revisar token_appdaemon')
    server.local_auth = ('Bearer '+local_key).encode()
    server.auth = ('Bearer '+key).encode()
    print('API intercambio lista:', SITE)
    server.serve_forever()
