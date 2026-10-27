"""Small, auditable Home Assistant tool set for user-requested multimedia actions."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import re
import unicodedata
import uuid
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


def connection():
    if os.getenv('SUPERVISOR_TOKEN'):
        return 'http://supervisor/core', os.environ['SUPERVISOR_TOKEN']
    base = (os.getenv('HA_URL') or 'http://homeassistant:8123').rstrip('/')
    if base.endswith('/api'):
        base = base[:-4]
    token = os.getenv('HA_TOKEN') or os.getenv('HOME_ASSISTANT_TOKEN') or os.getenv('SUPERVISOR_TOKEN')
    if not token:
        raise RuntimeError('No hay una conexión autenticada con Home Assistant')
    return base, token


async def ha_request(method, path, data=None):
    base, token = connection()
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.request(method, base + '/api' + path, headers={'Authorization': 'Bearer ' + token}, json=data)
        if response.status_code >= 400:
            raise RuntimeError(f'Home Assistant rechazó la petición (HTTP {response.status_code})')
        return response.json()


def folded(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value.casefold()) if not unicodedata.combining(c))


def audit(record):
    path = os.getenv('CODEXON_MULTIMEDIA_ACTION_LOG')
    if not path:
        raise RuntimeError('Falta el registro de acciones; no se ejecutará la orden')
    record = {**record, 'at': dt.datetime.now(dt.UTC).isoformat()}
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


mcp = FastMCP('multimedia_home_assistant', log_level='ERROR')


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def find_entities(search: str = '', domain: str = '', offset: int = 0) -> dict:
    """Busca entidades REALES y su estado. Usa varias búsquedas si el nombre es ambiguo. Devuelve hasta 40, con paginación."""
    states = await ha_request('GET', '/states')
    terms = folded(search).split()
    matches = [e for e in states if (not domain or e['entity_id'].startswith(domain + '.'))
               and all(t in folded(e['entity_id'] + ' ' + str(e['attributes'].get('friendly_name', ''))) for t in terms)]
    matches.sort(key=lambda e: e['entity_id'])
    offset = max(0, int(offset))
    return {'total': len(matches), 'next_offset': offset + 40 if len(matches) > offset + 40 else None,
            'entities': [{'entity_id': e['entity_id'], 'name': e['attributes'].get('friendly_name'), 'state': e['state'],
                          'attributes': {k: v for k, v in e['attributes'].items() if k in ('device_class', 'supported_features', 'options', 'min', 'max', 'unit_of_measurement')}}
                         for e in matches[offset:offset + 40]]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def get_services(domain: str) -> dict:
    """Consulta las acciones disponibles y sus campos en un dominio de Home Assistant, antes de ejecutarlas."""
    services = await ha_request('GET', '/services')
    return next((row for row in services if row['domain'] == domain), {'domain': domain, 'services': {}})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def call_service(domain: str, service: str, entity_ids: list[str], reason: str, data: dict | None = None) -> dict:
    """Ejecuta una acción solicitada por el usuario. Usa solo entidades reales consultadas, indica el motivo y no inventes objetivos. Registra el resultado real de la API."""
    if not re.fullmatch(r'[a-z_]+', domain) or not re.fullmatch(r'[a-z0-9_]+', service):
        raise ValueError('Dominio o servicio no válido')
    if not reason.strip():
        raise ValueError('Debes indicar qué petición del usuario autoriza la acción')
    data = dict(data or {})
    if any(key in data for key in ('entity_id', 'area_id', 'device_id', 'target')):
        raise ValueError('Indica los objetivos exclusivamente mediante entity_ids')
    services = await ha_request('GET', '/services')
    metadata = next((row['services'].get(service) for row in services if row['domain'] == domain), None)
    if metadata is None:
        raise ValueError('El servicio no existe en Home Assistant')
    states = {e['entity_id']: e for e in await ha_request('GET', '/states')}
    if len(entity_ids) > 100 or any(e not in states for e in entity_ids):
        raise ValueError('Hay entidades desconocidas o demasiados objetivos')
    if 'target' in metadata and not entity_ids:
        raise ValueError('Este servicio necesita entidades concretas como objetivo')
    payload = {**data, **({'entity_id': entity_ids} if entity_ids else {})}
    record = {'id': uuid.uuid4().hex, 'domain': domain, 'service': service, 'entity_ids': entity_ids,
              'reason': reason[:1000], 'data': data, 'status': 'requested'}
    audit(record)  # Persist before any side effect; a timeout must not look like an unattempted action.
    try:
        await ha_request('POST', f'/services/{domain}/{service}', payload)
    except Exception as exc:
        audit({**record, 'status': 'error_or_unknown', 'error': str(exc)})
        raise RuntimeError('La acción falló o no pudo verificarse; consulta su registro antes de repetirla.') from exc
    audit({**record, 'status': 'accepted'})
    observed = []
    for entity in entity_ids:
        try:
            state = await ha_request('GET', '/states/' + entity)
            observed.append({'entity_id': entity, 'state': state['state']})
        except (RuntimeError, httpx.HTTPError):
            observed.append({'entity_id': entity, 'state': 'no_verificado'})
    result = {**record, 'status': 'accepted', 'observed_states': observed}
    audit(result)
    return result


if __name__ == '__main__':
    mcp.run(transport='stdio')
