"""Bounded local image retention and Home Assistant browsing controls."""
import contextlib,datetime as dt,json,math,re,time
from pathlib import Path
from zoneinfo import ZoneInfo

RETENTION_ENTITY='input_number.codexon_imagenes_conservacion_dias'
FROM_ENTITY='input_datetime.codexon_imagenes_desde'
TO_ENTITY='input_datetime.codexon_imagenes_hasta'
SHOW_ENTITY='input_button.codexon_mostrar_imagenes'
CONTROLS={RETENTION_ENTITY,FROM_ENTITY,TO_ENTITY,SHOW_ENTITY}
RETENTION_KEY='security_visual.image_retention_days'
MANAGED_IMAGE=re.compile(r'\d+-[a-f0-9]{64}\.(?:jpg|png)')

def put_setting(conn,key,value):
 encoded=value if isinstance(value,str) else json.dumps(value)
 row=conn.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
 if row and row[0]==encoded:return
 conn.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)',(key,encoded,dt.datetime.now(dt.UTC).isoformat()));conn.commit()

def get_setting(conn,key,default=None):
 row=conn.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone();return row[0] if row and row[0] is not None else default

def validated_days(value):
 if isinstance(value,bool):raise ValueError('El plazo debe ser un número entero entre 1 y 90 días.')
 try:number=float(value)
 except (TypeError,ValueError):raise ValueError('El plazo debe ser un número entero entre 1 y 90 días.')
 if not math.isfinite(number) or not number.is_integer() or not 1<=number<=90:raise ValueError('El plazo debe ser un número entero entre 1 y 90 días.')
 return int(number)

def retention_days(conn):
 try:return validated_days(get_setting(conn,RETENTION_KEY,'15'))
 except ValueError:return 15

def date_range(from_date=None,to_date=None,timezone='Europe/Madrid',now=None):
 tz=ZoneInfo(timezone);today=(now or dt.datetime.now(dt.UTC)).astimezone(tz).date()
 if (from_date is None)!=(to_date is None):raise ValueError('Selecciona las dos fechas.')
 start_text=from_date or (today-dt.timedelta(days=1)).isoformat();end_text=to_date or today.isoformat()
 if not all(isinstance(x,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',x) for x in [start_text,end_text]):raise ValueError('Las fechas deben tener formato AAAA-MM-DD.')
 try:start=dt.date.fromisoformat(start_text);end=dt.date.fromisoformat(end_text)
 except ValueError:raise ValueError('Fecha no válida.')
 if start>end:raise ValueError('La fecha Desde debe ser anterior o igual a Hasta.')
 begin=dt.datetime.combine(start,dt.time(),tzinfo=tz).astimezone(dt.UTC)
 finish=dt.datetime.combine(end+dt.timedelta(days=1),dt.time(),tzinfo=tz).astimezone(dt.UTC)
 return {'from_date':start_text,'to_date':end_text,'timezone':timezone,'start_timestamp':begin.timestamp(),'end_timestamp':finish.timestamp()}

def image_files(root):
 if not root.exists():return
 for path in root.iterdir():
  if path.is_symlink() or not MANAGED_IMAGE.fullmatch(path.name):continue
  with contextlib.suppress(FileNotFoundError):
   if path.is_file():yield path,path.stat()

def archive_options(conn):
 try:
  config=json.loads(get_setting(conn,'security_visual.legacy_archive','null'))
  if not isinstance(config,dict) or config.get('root')!='/media':return {}
  cameras=config.get('cameras')
  if not isinstance(cameras,list) or not all(isinstance(n,str) and re.fullmatch(r'[a-z0-9_]+',n) for n in cameras):return {}
  return {'archive_root':Path('/media'),'archive_cameras':tuple(cameras)}
 except (ValueError,TypeError):return {}

def archive_files(root,cameras):
 if root is None or not root.exists():return
 pattern=re.compile(r'security_([a-z0-9_]+)_(\d{8}_\d{6})\.jpg')
 for path in root.iterdir():
  match=pattern.fullmatch(path.name)
  if not match or match[1] not in cameras or path.is_symlink():continue
  try:dt.datetime.strptime(match[2],'%Y%m%d_%H%M%S')
  except ValueError:continue
  with contextlib.suppress(FileNotFoundError):
   if path.is_file():yield path,path.stat()

def image_storage(root,days,*,archive_root=None,archive_cameras=()):
 files=list(image_files(root));archives=list(archive_files(archive_root,archive_cameras))
 return {'retention_days':days,'files':len(files)+len(archives),'bytes':sum(s.st_size for _,s in files+archives),'codexon_files':len(files),'appdaemon_files':len(archives)}

def cleanup_images(root,days,now=None,*,archive_root=None,archive_cameras=()):
 days=validated_days(days);cutoff=(time.time() if now is None else now)-days*86400;removed=0;freed=0
 for path,stat in list(image_files(root))+list(archive_files(archive_root,archive_cameras)):
  if stat.st_mtime>=cutoff:continue
  try:path.unlink()
  except FileNotFoundError:continue
  removed+=1;freed+=stat.st_size
 return {'deleted_files':removed,'freed_bytes':freed,**image_storage(root,days,archive_root=archive_root,archive_cameras=archive_cameras)}

def sync_controls(conn,states):
 for entity in [RETENTION_ENTITY,FROM_ENTITY,TO_ENTITY]:
  state=states.get(entity,{}).get('state')
  try:
   if entity==RETENTION_ENTITY:
    days=validated_days(state)
    if days!=retention_days(conn):put_setting(conn,'security_visual.storage_cleanup_requested',dt.datetime.now(dt.UTC).isoformat())
    put_setting(conn,RETENTION_KEY,str(days))
   elif state and re.fullmatch(r'\d{4}-\d{2}-\d{2}',state):
    dt.date.fromisoformat(state);put_setting(conn,'security_visual.control.'+entity,state)
  except ValueError:continue

def offer_control(conn,event):
 data=event.get('data') or {};entity=data.get('entity_id')
 if entity not in CONTROLS:return False
 new=data.get('new_state') or {};old=data.get('old_state') or {}
 if new.get('state')==old.get('state'):return True
 if entity!=SHOW_ENTITY:sync_controls(conn,{entity:new});return True
 requested=new.get('state')
 try:
  parsed=dt.datetime.fromisoformat(str(requested).replace('Z','+00:00'))
  if parsed.tzinfo is None:return True
  start=get_setting(conn,'security_visual.control.'+FROM_ENTITY);end=get_setting(conn,'security_visual.control.'+TO_ENTITY)
  if not start or not end:return True
  date_range(start,end)
  put_setting(conn,'security_visual.browse_selection',{'from_date':start,'to_date':end,'requested_at':requested})
 except ValueError:pass
 return True
