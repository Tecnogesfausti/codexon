import http.client
import json
import logging
import os
import socket
import sqlite3
import ssl
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from ajustes import CONFIG, DB, ROOT, SITE, PEER, SHARED

HOST = CONFIG.get('peer_host')

def peer(method, route, payload=None):
    if not HOST:
        raise ValueError('Configurar peer_host para enviar')
    conn = http.client.HTTPSConnection(HOST, timeout=8)
    try:
        try:
            sock = socket.create_connection((HOST,443),8)
        except socket.gaierror:
            sock = socket.create_connection((CONFIG['peer_ip'],443),8)
        conn.sock = ssl.create_default_context().wrap_socket(sock,server_hostname=HOST)
        key = SHARED or (ROOT / 'token_saliente').read_text().strip()
        conn.request(method, '/intercambio/v2/messages'+route,
                     json.dumps(payload).encode() if payload else None,
                     {'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        r = conn.getresponse()
        if r.status not in (200,201):
            raise RuntimeError('HTTP '+str(r.status))
        return json.load(r)
    finally:
        conn.close()

def deliver(db, msg, temperature):
    mid, thread, body = (msg[k] for k in ('id','thread_id','body'))
    saved = db.execute('SELECT reply,done FROM listener WHERE id=?',(mid,)).fetchone()
    if saved and saved[1]:
        return
    if saved:
        reply = saved[0]
    else:
        question = body.lower().strip(' ¡!¿?.\n')
        reply = ''
        if question in ('hola','buenos días','buenas'):
            reply = f'{SITE} ha recibido tu saludo.'
        elif ('temperatura' in question and ('?' in body or question.startswith(('dime','consulta','temperatura','qué','cuál')))):
            sample = temperature()
            reply = f"{SITE}: temperatura exterior {sample['value']} °C ({sample['observed_at']}, calidad {sample['quality']})." if sample['value'] is not None else f'{SITE}: temperatura exterior sin lectura disponible.'
        db.execute('INSERT INTO listener VALUES (?,?,0)',(mid,reply))
        db.commit()
    if reply:
        rid = str(uuid.uuid5(uuid.NAMESPACE_URL,'intercambio/'+SITE+'/'+mid))
        ack = peer('POST','',dict(id=rid,thread_id=thread,body=reply))
        if ack.get('id') != rid:
            raise ValueError('Acuse incorrecto')
    else:
        req = Request('http://supervisor/core/api/services/persistent_notification/create',
                      data=json.dumps(dict(notification_id='intercambio_'+mid,title='Mensaje de '+PEER,message=body)).encode(),
                      headers={'Authorization':'Bearer '+os.environ['SUPERVISOR_TOKEN'],'Content-Type':'application/json'})
        with urlopen(req,timeout=8): pass
    db.execute('UPDATE listener SET done=1 WHERE id=?',(mid,))
    db.commit()

def listen(temperature):
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS listener(id TEXT PRIMARY KEY,reply TEXT,done INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS cursor_remoto(slot INTEGER PRIMARY KEY,id TEXT)')
    while True:
        try:
            for msg in db.execute('SELECT * FROM messages WHERE id NOT IN (SELECT id FROM listener WHERE done=1) ORDER BY rowid LIMIT 100').fetchall():
                deliver(db,msg,temperature)
            if not HOST:
                time.sleep(20)
                continue
            cursor = db.execute('SELECT id FROM cursor_remoto WHERE slot=1').fetchone()
            query = '?limit=100' + ('&after='+str(uuid.UUID(cursor[0])) if cursor else '')
            for msg in peer('GET',query)['messages']:
                if msg.get('sender')==PEER and msg.get('recipient')==SITE:
                    db.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?)",
                               (msg["id"],msg["thread_id"],msg["body"],msg["created_at"]))
                    db.commit()
                    deliver(db,msg,temperature)
                db.execute('INSERT OR REPLACE INTO cursor_remoto VALUES (1,?)',(msg['id'],))
                db.commit()
        except Exception as e:
            logging.warning('Espera del buzón: %s', type(e).__name__)
        time.sleep(20)
