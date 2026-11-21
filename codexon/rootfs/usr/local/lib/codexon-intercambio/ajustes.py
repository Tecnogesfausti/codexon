import json
import re
from pathlib import Path

ROOT = Path('/addon_config/intercambio')
ROOT.mkdir(mode=0o700, exist_ok=True)
OPTIONS = json.loads(Path('/data/options.json').read_text())
legacy = ROOT / 'config.json'
CONFIG = json.loads(legacy.read_text()) if legacy.exists() else {}
for field in ('site', 'peer', 'peer_host', 'peer_ip', 'temperature_entity', 'timezone'):
    if OPTIONS.get('intercambio_' + field):
        CONFIG[field] = OPTIONS['intercambio_' + field]
SITE = CONFIG['site']
PEER = CONFIG.get('peer', 'remoto')
CONFIG.setdefault('timezone', 'UTC')
DB = ROOT / 'messages.sqlite3'
SHARED = OPTIONS.get('intercambio_token', '').strip()
if not SHARED and not legacy.exists():
    raise ValueError('Configura intercambio_token')
if SHARED and len(SHARED) < 32:
    raise ValueError('El token compartido requiere al menos 32 caracteres')
ACTIONS = set(OPTIONS.get('intercambio_scripts', []))
if any(not re.fullmatch(r'script\.[a-z0-9_]+', item) for item in ACTIONS):
    raise ValueError('intercambio_scripts requiere entidades script de Home Assistant')
