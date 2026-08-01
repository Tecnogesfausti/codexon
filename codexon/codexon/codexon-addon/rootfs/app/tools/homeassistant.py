from __future__ import annotations

import datetime as dt
import asyncio
import json
import math
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.common import compact_json


LAST_INTERVAL_ACTIONS: dict[tuple[str, float], float] = {}
INTERVAL_ACTION_DEDUPE_SECONDS = 20.0


def ha_base_url(context: Any) -> str:
    base_url = getattr(context, "ha_base_url", None)
    if not base_url:
        raise ValueError("Falta URL base de Home Assistant")
    return str(base_url).rstrip("/")


def ha_headers(context: Any) -> dict[str, str]:
    token = getattr(context, "ha_token", None)
    if not token:
        raise ValueError("Falta HA_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def httpx_module(context: Any) -> Any:
    httpx = getattr(context, "httpx", None)
    if httpx is None:
        raise RuntimeError("httpx no esta disponible")
    return httpx


def ha_websocket_url(context: Any) -> str:
    base_url = ha_base_url(context)
    if base_url.startswith("https://"):
        websocket_base = "wss://" + base_url.removeprefix("https://")
    elif base_url.startswith("http://"):
        websocket_base = "ws://" + base_url.removeprefix("http://")
    else:
        raise ValueError("La URL base de Home Assistant debe usar http o https")
    if websocket_base.rstrip("/").endswith("/core"):
        return websocket_base.rstrip("/") + "/websocket"
    return websocket_base.rstrip("/") + "/api/websocket"


async def ha_websocket_command(context: Any, command: dict[str, Any]) -> Any:
    try:
        import websockets
    except ModuleNotFoundError as exc:
        raise RuntimeError("websockets no esta disponible") from exc

    token = getattr(context, "ha_token", None)
    if not token:
        raise ValueError("Falta HA_TOKEN")
    async with websockets.connect(ha_websocket_url(context), open_timeout=15, close_timeout=5) as websocket:
        auth_required = json.loads(await websocket.recv())
        if auth_required.get("type") != "auth_required":
            raise RuntimeError(f"Respuesta WebSocket inesperada: {auth_required.get('type')}")
        await websocket.send(json.dumps({"type": "auth", "access_token": token}))
        auth_result = json.loads(await websocket.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"Autenticacion WebSocket fallida: {auth_result.get('message') or auth_result.get('type')}")

        request = {"id": 1, **command}
        await websocket.send(json.dumps(request))
        while True:
            response = json.loads(await websocket.recv())
            if response.get("id") != 1:
                continue
            if not response.get("success"):
                error = response.get("error") or {}
                raise RuntimeError(str(error.get("message") or error.get("code") or "Comando WebSocket fallido"))
            return response.get("result")


def is_tts_service(domain: str, service: str) -> bool:
    return domain.strip().lower() == "tts" or "tts" in service.strip().lower()


def is_notify_service(domain: str, service: str) -> bool:
    return domain.strip().lower() == "notify" or service.strip().lower().startswith("mobile_app_")


def validate_tts_service_call(domain: str, service: str, payload: dict[str, Any], confirm: bool) -> None:
    if not is_tts_service(domain, service):
        return
    message_folded = fold_text(payload.get("message", ""))
    entity_folded = fold_text(payload.get("entity_id", ""))
    if "nspanel" in message_folded and any(token in message_folded for token in ("nokia", "tono", "melodia", "timbre")):
        raise ValueError("Para tonos Nokia/RTTTL en NSPanel no uses TTS; usa el servicio RTTTL de ESPHome correspondiente con service_data.tone y confirm=true.")
    if "nspanel" in entity_folded and any(token in message_folded for token in ("nokia", "tono", "melodia", "timbre")):
        raise ValueError("Para tonos musicales del NSPanel no uses TTS; usa el servicio ESPHome RTTTL.")
    if domain.strip().lower() != "tts" or service.strip() != "google_translate_say":
        raise ValueError("Para TTS usa exactamente tts.google_translate_say.")
    if payload.get("cache") is not False:
        raise ValueError("Para TTS usa cache=false.")
    if payload.get("language") != "es":
        raise ValueError("Para TTS usa language=es.")
    message = str(payload.get("message", "") or "").strip()
    if not message:
        raise ValueError("Para TTS message es obligatorio.")
    entity_id = payload.get("entity_id")
    entity_ids = entity_id if isinstance(entity_id, list) else [entity_id]
    if not entity_ids or any(not isinstance(item, str) or not item.startswith("media_player.") for item in entity_ids):
        raise ValueError("Para TTS entity_id debe ser un media_player disponible.")


def normalize_entity_text(value: str) -> str:
    text = value.strip().lower()
    if "." in text:
        text = text.split(".", 1)[1]
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def entity_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize_entity_text(value)).strip("_")


def fold_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(char for char in text if not unicodedata.combining(char))


def normalize_history_iso(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Madrid"))
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def search_tokens(value: str) -> list[str]:
    stopwords = {"de", "del", "la", "el", "los", "las", "en", "y", "o", "u", "the", "of", "or"}
    return [token for token in re.split(r"[^a-z0-9]+", fold_text(value)) if token and token not in stopwords]


DOMAIN_QUERY_TERMS: dict[str, set[str]] = {
    "switch": {"interruptor", "interruptores", "switch", "switches", "rele", "reles", "relay", "relays"},
    "light": {"luz", "luces", "lampara", "lamparas", "bombilla", "bombillas", "light", "lights"},
    "sensor": {"sensor", "sensores"},
    "binary_sensor": {"sensor", "sensores", "binario", "binarios"},
    "media_player": {"altavoz", "altavoces", "pantalla", "pantallas", "media", "player"},
    "climate": {"clima", "termostato", "termostatos", "climate"},
}


def domain_query_terms(domains: list[str]) -> set[str]:
    terms: set[str] = set()
    for domain in domains:
        terms.update(DOMAIN_QUERY_TERMS.get(domain, set()))
    return terms


def normalize_query_for_domains(query: str, domains: list[str]) -> str:
    """Drop generic category words already expressed by the domain filter."""
    removable = {singularize_token(token) for token in domain_query_terms(domains)}
    if not removable:
        return query
    kept = [token for token in search_tokens(query) if singularize_token(token) not in removable]
    return " ".join(kept)


def physical_light_action_query(query: str) -> bool:
    folded = fold_text(query).lower()
    return bool(
        re.search(r"\b(enciende|encender|apaga|apagar|prende|pon|poner|activa|activar|desactiva|desactivar)\b", folded)
        and re.search(r"\bluz(?:es)?\b", folded)
    )


def expanded_search_domains(query: str, domain: str) -> list[str]:
    domains = [item.strip() for item in re.split(r"[, ]+", domain) if item.strip()]
    if "light" in domains and "switch" not in domains and physical_light_action_query(query):
        domains.append("switch")
    return domains


def singularize_token(token: str) -> str:
    for suffix in ("es", "s"):
        if len(token) > 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def token_matches(needle: str, haystack_tokens: set[str]) -> bool:
    needle = singularize_token(needle)
    for token in haystack_tokens:
        candidate = singularize_token(token)
        if needle == candidate:
            return True
        if len(needle) >= 4 and len(candidate) >= 4 and (candidate.startswith(needle) or needle.startswith(candidate)):
            return True
    return False


def state_search_fields(state: dict[str, Any]) -> tuple[str, str, str]:
    entity_id = str(state.get("entity_id") or "")
    attrs = state.get("attributes") or {}
    registry = state.get("_registry") if isinstance(state.get("_registry"), dict) else {}
    friendly_name = str(attrs.get("friendly_name") or "")
    aliases = " ".join(
        str(alias)
        for alias in [
            *(attrs.get("aliases") or []),
            *(registry.get("aliases") or []),
            *(registry.get("area_aliases") or []),
        ]
        if alias
    )
    return entity_id, friendly_name, " ".join(
        str(part)
        for part in (
            attrs.get("device_class", ""),
            attrs.get("unit_of_measurement", ""),
            attrs.get("area_id", ""),
            registry.get("area_id", ""),
            registry.get("area_name", ""),
            registry.get("device_name", ""),
            attrs.get("icon", ""),
            state.get("state", ""),
            aliases,
        )
    )


def score_state_match(state: dict[str, Any], query: str) -> tuple[int, list[str]]:
    query_tokens = search_tokens(query)
    entity_id, friendly_name, extra = state_search_fields(state)
    if not query_tokens:
        score = 1
        registry = state.get("_registry") if isinstance(state.get("_registry"), dict) else {}
        searchable_tokens = set(search_tokens(" ".join((entity_id, friendly_name, extra))))
        if registry.get("area_name"):
            score += 3
        if entity_id.startswith("switch."):
            physical_switch_tokens = {
                "rele",
                "relay",
                "luz",
                "luces",
                "pia",
                "enchufe",
                "termo",
                "sirena",
                "cancela",
                "motor",
                "riego",
                "persiana",
                "persianas",
                "alarma",
            }
            config_switch_tokens = {
                "pre",
                "release",
                "restart",
                "update",
                "upgrade",
                "firmware",
                "hacs",
                "card",
                "discovery",
                "diagnose",
                "mode",
                "notifications",
                "sync",
            }
            if searchable_tokens & physical_switch_tokens:
                score += 8
            if searchable_tokens & config_switch_tokens:
                score -= 20
        if str(state.get("state", "")).lower() in {"unknown", "unavailable"}:
            score -= 80
        return score, []
    entity_slug_text = entity_slug(entity_id)
    friendly_slug = entity_slug(friendly_name)
    query_slug = entity_slug(query)
    searchable_tokens = set(search_tokens(" ".join((entity_id, friendly_name, extra))))

    score = 0
    matched: list[str] = []
    if query_slug and query_slug == entity_slug_text:
        score += 100
    if query_slug and query_slug == friendly_slug:
        score += 95
    if query_slug and (query_slug in entity_slug_text or query_slug in friendly_slug):
        score += 25
    for token in query_tokens:
        if token_matches(token, searchable_tokens):
            matched.append(token)
            score += 10
            if token in search_tokens(friendly_name):
                score += 3
            if token in search_tokens(entity_id):
                score += 2
    if matched and len(matched) == len(query_tokens):
        score += 20
    if str(state.get("state", "")).lower() in {"unknown", "unavailable"}:
        score -= 80
    location_intent = {"casa", "interior", "dentro", "inside", "indoor"} & set(query_tokens)
    temperature_intent = {"temperatura", "temperature", "temp", "calor", "frio"} & set(query_tokens)
    registry = state.get("_registry") if isinstance(state.get("_registry"), dict) else {}
    attrs = state.get("attributes") or {}
    device_class = str(attrs.get("device_class") or registry.get("device_class") or "").lower()
    equipment_text = " ".join(str(part) for part in (entity_id, friendly_name, registry.get("device_name", "")))
    equipment_tokens = set(search_tokens(equipment_text))
    if temperature_intent:
        if device_class == "temperature":
            score += 18
        elif any(token in {"temperature", "temperatura", "temp"} for token in searchable_tokens):
            score += 8
        else:
            score -= 40
        non_ambient_tokens = {
            "termo",
            "settemp",
            "set",
            "objetivo",
            "battery",
            "bateria",
            "internal",
            "interna",
            "externa",
            "external",
            "forecast",
            "feeling",
            "sensacion",
            "daily",
            "riego",
            "regleta",
        }
        if equipment_tokens & non_ambient_tokens:
            score -= 22
    if location_intent:
        location_text = " ".join(
            str(part)
            for part in (
                registry.get("area_id", ""),
                registry.get("area_name", ""),
                registry.get("device_name", ""),
                friendly_name,
                entity_id,
            )
        )
        location_tokens = set(search_tokens(location_text))
        outdoor_tokens = {"terreno", "exterior", "outside", "outdoor", "riego", "sol", "jardin", "huerto", "orihuela"}
        indoor_tokens = {"salon", "comedor", "cocina", "dormitorio", "habitacion", "cuarto", "oficina", "interior", "casa"}
        if location_tokens & indoor_tokens:
            score += 35
        if location_tokens & outdoor_tokens:
            score -= 25
    return score, matched


def state_row(state: dict[str, Any], *, score: int | None = None, matched_terms: list[str] | None = None) -> dict[str, Any]:
    attrs = state.get("attributes") or {}
    registry = state.get("_registry") if isinstance(state.get("_registry"), dict) else {}
    row: dict[str, Any] = {
        "entity_id": state.get("entity_id"),
        "state": state.get("state"),
        "friendly_name": attrs.get("friendly_name"),
        "area_id": registry.get("area_id"),
        "area_name": registry.get("area_name"),
        "device_name": registry.get("device_name"),
        "device_class": attrs.get("device_class"),
        "unit": attrs.get("unit_of_measurement"),
        "last_changed": state.get("last_changed"),
        "last_updated": state.get("last_updated"),
        "aliases": registry.get("aliases") or [],
    }
    if score is not None:
        row["match_score"] = score
    if matched_terms is not None:
        row["matched_terms"] = matched_terms
    return row


def catalog_search_row(row: Any, *, score: int = 88, query: str = "") -> dict[str, Any]:
    aliases_raw = row["aliases"] if "aliases" in row.keys() else ""
    try:
        aliases = json.loads(aliases_raw) if isinstance(aliases_raw, str) and aliases_raw else []
    except json.JSONDecodeError:
        aliases = []
    terms = [term for term in re.split(r"\W+", fold_text(query).lower()) if len(term) >= 3]
    haystack = fold_text(" ".join(str(part or "") for part in (
        row["entity_id"], row["friendly_name"], row["area_name"], row["device_name"], aliases_raw
    ))).lower()
    matched_terms = [term for term in terms if term in haystack]
    return {
        "entity_id": row["entity_id"],
        "state": row["state"] if "state" in row.keys() else None,
        "friendly_name": row["friendly_name"],
        "area_id": None,
        "area_name": row["area_name"],
        "device_name": row["device_name"],
        "device_class": row["device_class"],
        "unit": row["unit"],
        "last_changed": None,
        "last_updated": row["updated_at"] if "updated_at" in row.keys() else None,
        "aliases": aliases,
        "match_score": score,
        "matched_terms": matched_terms,
        "source": "codexon_entity_catalog",
    }


def merge_learned_entity_rows(context: Any, rows: list[dict[str, Any]], *, query: str, domain: str, limit: int) -> list[dict[str, Any]]:
    memory = getattr(context, "memory", None)
    if memory is None or not hasattr(memory, "search_entity_catalog"):
        return rows
    try:
        learned = memory.search_entity_catalog(query, domain=domain, limit=max(limit, 12))
    except Exception:
        return rows
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for learned_row in learned:
        entity_id = str(learned_row["entity_id"] or "")
        if not entity_id or entity_id in seen:
            continue
        merged.append(catalog_search_row(learned_row, query=query))
        seen.add(entity_id)
    for row in rows:
        entity_id = str(row.get("entity_id") or "")
        if not entity_id or entity_id in seen:
            continue
        merged.append(row)
        seen.add(entity_id)
    return merged[:limit]


async def fetch_ha_states(context: Any) -> list[dict[str, Any]]:
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/states", headers=ha_headers(context))
        response.raise_for_status()
        states = response.json()
    if not isinstance(states, list):
        return []
    enrich_states_with_registry(context, states)
    return states


def storage_roots(context: Any) -> list[Path]:
    roots = [Path(root) / ".storage" for root in getattr(context, "fs_roots", []) or []]
    for value in (
        os.environ.get("HA_CONFIG_DIR"),
        "/ha_config",
        "/config",
    ):
        if value:
            roots.append(Path(value) / ".storage")
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        text = str(root)
        if text not in seen:
            seen.add(text)
            unique.append(root)
    return unique


def read_storage_json(context: Any, filename: str) -> dict[str, Any]:
    for root in storage_roots(context):
        path = root / filename
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
            continue
    return {}


def registry_aliases(value: Any) -> list[str]:
    aliases: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                aliases.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("alias")
                if name:
                    aliases.append(str(name))
    return aliases


def load_location_registry(context: Any) -> dict[str, dict[str, Any]]:
    area_data = read_storage_json(context, "core.area_registry").get("data", {})
    device_data = read_storage_json(context, "core.device_registry").get("data", {})
    entity_data = read_storage_json(context, "core.entity_registry").get("data", {})
    areas = {
        str(area.get("id")): area
        for area in area_data.get("areas", [])
        if isinstance(area, dict) and area.get("id")
    }
    devices = {
        str(device.get("id")): device
        for device in device_data.get("devices", [])
        if isinstance(device, dict) and device.get("id")
    }
    entities = {
        str(entity.get("entity_id")): entity
        for entity in entity_data.get("entities", [])
        if isinstance(entity, dict) and entity.get("entity_id")
    }
    return {"areas": areas, "devices": devices, "entities": entities}


def enrich_states_with_registry(context: Any, states: list[dict[str, Any]]) -> None:
    registry = load_location_registry(context)
    areas = registry["areas"]
    devices = registry["devices"]
    entities = registry["entities"]
    for state in states:
        entity_id = str(state.get("entity_id") or "")
        entity = entities.get(entity_id, {})
        device = devices.get(str(entity.get("device_id") or ""), {})
        area_id = entity.get("area_id") or device.get("area_id")
        area = areas.get(str(area_id or ""), {})
        device_name = device.get("name_by_user") or device.get("name")
        state["_registry"] = {
            "area_id": area_id,
            "area_name": area.get("name"),
            "area_aliases": registry_aliases(area.get("aliases")),
            "device_id": entity.get("device_id"),
            "device_name": device_name,
            "aliases": [
                *registry_aliases(entity.get("aliases")),
                *registry_aliases(entity.get("aliases_v2")),
            ],
        }


def ranked_state_matches(
    states: list[dict[str, Any]],
    *,
    query: str,
    domain: str = "",
    limit: int = 20,
    include_unavailable: bool = True,
) -> list[dict[str, Any]]:
    rows: list[tuple[int, int, list[str], dict[str, Any]]] = []
    domains = expanded_search_domains(query, domain)
    query = normalize_query_for_domains(query, domains)
    for index, state in enumerate(states):
        entity_id = str(state.get("entity_id", ""))
        if domains and not any(entity_id.startswith(f"{item}.") for item in domains):
            continue
        if not include_unavailable and str(state.get("state", "")).lower() in {"unknown", "unavailable"}:
            continue
        score, matched_terms = score_state_match(state, query)
        if query and score <= 0:
            continue
        rows.append((score, index, matched_terms, state))
    rows.sort(key=lambda item: (-item[0], item[1]))
    return [state_row(state, score=score, matched_terms=matched_terms) for score, _, matched_terms, state in rows[:limit]]


async def resolve_entity_reference(
    context: Any,
    *,
    entity_id: str = "",
    query: str = "",
    domain: str = "",
    include_unavailable: bool = True,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    reference = str(entity_id or query or "").strip()
    if not reference:
        return "", warnings
    states = await fetch_ha_states(context)
    exact = [state for state in states if str(state.get("entity_id") or "") == reference]
    if exact:
        catalog_row = state_row(exact[0], score=100, matched_terms=search_tokens(reference))
        memory = getattr(context, "memory", None)
        if memory is not None and hasattr(memory, "upsert_entity_catalog"):
            memory.upsert_entity_catalog([catalog_row], source="exact_resolution")
        search_domain = domain or (reference.split(".", 1)[0] if "." in reference else "")
        if memory is not None and hasattr(memory, "add_entity_resolution"):
            memory.add_entity_resolution(query=reference, domain=search_domain, resolved_entity_id=reference, candidates=[catalog_row], source="exact_resolution")
        return reference, warnings
    search_domain = domain
    if not search_domain and "." in reference and reference == reference.lower():
        search_domain = reference.split(".", 1)[0]
    matches = ranked_state_matches(states, query=reference, domain=search_domain, limit=5, include_unavailable=include_unavailable)
    if not matches:
        lowered = reference.lower()
        if lowered != reference:
            warnings.append(f"entity_lowercased:{reference}->{lowered}")
            return lowered, warnings
        return reference if entity_id and "." in reference else "", warnings
    first = matches[0]
    second_score = int(matches[1].get("match_score") or 0) if len(matches) > 1 else -1
    first_score = int(first.get("match_score") or 0)
    resolved = str(first.get("entity_id") or "")
    if len(matches) > 1 and (first_score == second_score or (first_score < 90 and first_score - second_score < 12)):
        options = ", ".join(f"{item.get('entity_id')} ({item.get('friendly_name')})" for item in matches[:5])
        raise ValueError(f"Entidad ambigua para '{reference}'. Candidatos: {options}")
    memory = getattr(context, "memory", None)
    if memory is not None and hasattr(memory, "upsert_entity_catalog"):
        memory.upsert_entity_catalog(matches, source="resolver")
    if memory is not None and hasattr(memory, "add_entity_resolution"):
        memory.add_entity_resolution(query=reference, domain=search_domain or domain, resolved_entity_id=resolved, candidates=matches, source="resolver")
    if entity_id and resolved != entity_id:
        warnings.append(f"entity_resolved:{entity_id}->{resolved}")
    return resolved, warnings


async def resolve_payload_entity_ids(context: Any, payload: dict[str, Any], *, domain: str = "") -> list[str]:
    warnings: list[str] = []
    target = payload.pop("target", None)
    if isinstance(target, dict):
        for key, value in target.items():
            payload.setdefault(key, value)
    for container in (payload,):
        if not isinstance(container, dict) or "entity_id" not in container:
            continue
        raw = container.get("entity_id")
        raw_items = raw if isinstance(raw, list) else [raw]
        if not raw_items or any(not isinstance(item, str) for item in raw_items):
            continue
        resolved_items: list[str] = []
        for item in raw_items:
            resolved, item_warnings = await resolve_entity_reference(context, entity_id=item, domain=domain, include_unavailable=False)
            resolved_items.append(resolved or item)
            warnings.extend(item_warnings)
        container["entity_id"] = resolved_items if isinstance(raw, list) else resolved_items[0]
    return warnings


async def get_tts_media_player_candidates(context: Any) -> list[dict[str, Any]]:
    states = await fetch_ha_states(context)

    candidates: list[dict[str, Any]] = []
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if not entity_id.startswith("media_player."):
            continue
        current_state = str(state.get("state", "") or "").lower()
        if current_state in {"unavailable", "unknown"}:
            continue
        attrs = state.get("attributes") or {}
        candidates.append(
            {
                "entity_id": entity_id,
                "state": current_state,
                "friendly_name": attrs.get("friendly_name"),
                "volume_level": attrs.get("volume_level"),
                "is_volume_muted": attrs.get("is_volume_muted"),
            }
        )
    return candidates


def is_audible_tts_candidate(candidate: dict[str, Any]) -> bool:
    if candidate.get("is_volume_muted") is True:
        return False
    volume = candidate.get("volume_level")
    return not isinstance(volume, (int, float)) or volume > 0


def resolve_tts_media_player_id(entity_id: str, candidates: list[dict[str, Any]]) -> str:
    wanted_slug = entity_slug(entity_id)
    wanted_text = normalize_entity_text(entity_id)
    for candidate in candidates:
        candidate_id = str(candidate.get("entity_id") or "")
        friendly_name = str(candidate.get("friendly_name") or "")
        if entity_id == candidate_id:
            return candidate_id
        if wanted_slug and wanted_slug in {entity_slug(candidate_id), entity_slug(friendly_name)}:
            return candidate_id
        if wanted_text and wanted_text in {normalize_entity_text(candidate_id), normalize_entity_text(friendly_name)}:
            return candidate_id
    return entity_id


async def resolve_tts_payload_entities(context: Any, payload: dict[str, Any]) -> None:
    target = payload.pop("target", None)
    if isinstance(target, dict):
        for key, value in target.items():
            payload.setdefault(key, value)
    raw_entity_id = payload.get("entity_id")
    entity_ids = raw_entity_id if isinstance(raw_entity_id, list) else [raw_entity_id]
    if not entity_ids or any(not isinstance(item, str) for item in entity_ids):
        return

    candidates = await get_tts_media_player_candidates(context)
    resolved = [resolve_tts_media_player_id(item, candidates) for item in entity_ids]
    blocked = [
        candidate
        for candidate in candidates
        if str(candidate.get("entity_id") or "") in resolved and not is_audible_tts_candidate(candidate)
    ]
    if blocked:
        blocked_text = ", ".join(
            f"{candidate.get('entity_id')} (mute={candidate.get('is_volume_muted')}, volume={candidate.get('volume_level')})"
            for candidate in blocked
        )
        raise ValueError(f"Destino TTS no audible: {blocked_text}. No lo uses salvo que el usuario pida subir volumen o quitar mute.")
    payload["entity_id"] = resolved if isinstance(raw_entity_id, list) else resolved[0]


def parse_ha_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def parse_local_time(value: str) -> dt.time:
    text = value.strip().lower().replace(" ", "")
    for suffix in ("am", "pm"):
        if text.endswith(suffix):
            base = text[: -len(suffix)]
            hour_text, _, minute_text = base.partition(":")
            hour = int(hour_text)
            minute = int(minute_text or "0")
            if suffix == "pm" and hour != 12:
                hour += 12
            if suffix == "am" and hour == 12:
                hour = 0
            return dt.time(hour=hour, minute=minute)
    hour_text, _, minute_text = text.partition(":")
    return dt.time(hour=int(hour_text), minute=int(minute_text or "0"))


def choose_nearest_history_point(points: list[dict[str, Any]], target: dt.datetime) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for point in useful_history_points(points):
        when = parse_ha_datetime(str(point.get("last_updated") or point.get("last_changed") or ""))
        if when is None:
            continue
        candidates.append((abs((when - target.astimezone(when.tzinfo)).total_seconds()), point))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def parse_ha_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def parse_local_time(value: str) -> dt.time:
    text = value.strip().lower().replace(" ", "")
    for suffix in ("am", "pm"):
        if text.endswith(suffix):
            base = text[: -len(suffix)]
            hour_text, _, minute_text = base.partition(":")
            hour = int(hour_text)
            minute = int(minute_text or "0")
            if suffix == "pm" and hour != 12:
                hour += 12
            if suffix == "am" and hour == 12:
                hour = 0
            return dt.time(hour=hour, minute=minute)
    hour_text, _, minute_text = text.partition(":")
    return dt.time(hour=int(hour_text), minute=int(minute_text or "0"))


def choose_nearest_history_point(points: list[dict[str, Any]], target: dt.datetime) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for point in useful_history_points(points):
        when = parse_ha_datetime(str(point.get("last_updated") or point.get("last_changed") or ""))
        if when is None:
            continue
        candidates.append((abs((when - target.astimezone(when.tzinfo)).total_seconds()), point))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def entity_search_text(entity_id: str) -> str:
    text = entity_id.strip().lower()
    if "." in text:
        text = text.split(".", 1)[1]
    for suffix in ("_temperatura", "_temperature", "_temp"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.replace("_", " ").strip()


async def resolve_entity_for_history_around_time(context: Any, args: dict[str, Any]) -> tuple[str, list[str]]:
    warnings: list[str] = []
    entity_id = str(args.get("entity_id", "") or "").strip()
    if entity_id:
        domain = str(args.get("domain") or entity_id.split(".", 1)[0] if "." in entity_id else "sensor")
        resolved, warnings = await resolve_entity_reference(context, entity_id=entity_id, query=entity_search_text(entity_id), domain=domain)
        return resolved, warnings
    resolved, warnings = await resolve_entity_reference(
        context,
        query=str(args.get("query", "") or ""),
        domain=str(args.get("domain", "sensor") or "sensor"),
    )
    if resolved:
        return resolved, warnings
    return "", warnings


async def ha_get_states(context: Any, args: dict[str, Any]) -> str:
    query = str(args.get("query", "") or "").strip().lower()
    domain = str(args.get("domain", "") or "").strip().lower()
    expanded_domain = ",".join(expanded_search_domains(query, domain))
    limit = max(1, min(int(args.get("limit", 100) or 100), 500))
    rows = ranked_state_matches(await fetch_ha_states(context), query=query, domain=expanded_domain, limit=limit)
    memory = getattr(context, "memory", None)
    if memory is not None and hasattr(memory, "upsert_entity_catalog"):
        memory.upsert_entity_catalog(rows, source="ha_get_states")
    rows = merge_learned_entity_rows(context, rows, query=query, domain=expanded_domain, limit=limit)
    return compact_json(rows, max_chars=20000)


async def ha_get_state(context: Any, args: dict[str, Any]) -> str:
    entity_id = str(args.get("entity_id", "") or "").strip()
    query = str(args.get("query", "") or "").strip()
    domain = str(args.get("domain", "") or "").strip()
    if not entity_id and not query:
        raise ValueError("entity_id es obligatorio")
    resolved_entity_id, warnings = await resolve_entity_reference(context, entity_id=entity_id, query=query, domain=domain)
    if not resolved_entity_id:
        raise ValueError(f"No se encontro entidad para '{entity_id or query}'")
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/states/{resolved_entity_id}", headers=ha_headers(context))
        response.raise_for_status()
        data = response.json()
    if warnings:
        data = {"resolved_entity_id": resolved_entity_id, "warnings": warnings, "state": data}
    return compact_json(data, max_chars=16000)


async def fetch_ha_state(context: Any, entity_id: str) -> dict[str, Any]:
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/states/{entity_id}", headers=ha_headers(context))
        response.raise_for_status()
        data = response.json()
    return data if isinstance(data, dict) else {}


async def ha_search_entities(context: Any, args: dict[str, Any]) -> str:
    return await ha_get_states(
        context,
        {"query": args.get("query", ""), "domain": args.get("domain", ""), "limit": args.get("limit", 20)},
    )


SECURITY_ACTIVITY_WINDOWS = {
    "5min": ("count_5min", "ultimos 5 minutos"),
    "1h": ("count_1h", "ultima hora"),
    "24h": ("count_24h", "ultimas 24 horas"),
}


def security_activity_window(value: str) -> tuple[str, str, str]:
    folded = fold_text(value or "")
    if any(term in folded for term in ("5min", "5 min", "cinco", "reciente", "ahora", "actual")):
        key = "5min"
    elif any(term in folded for term in ("1h", "1 h", "hora", "ultima hora")):
        key = "1h"
    else:
        key = "24h"
    count_key, label = SECURITY_ACTIVITY_WINDOWS[key]
    return key, count_key, label


def security_area_terms(area: str) -> set[str]:
    folded = fold_text(area or "")
    terms = {term for term in re.split(r"\W+", folded) if term}
    if not terms:
        return set()
    expanded = set(terms)
    if terms & {"terreno", "huerta", "huerto", "exterior", "fuera", "campo"}:
        expanded.update({"terreno", "huerta", "huerto", "exterior", "fuera", "cancela", "garrofera", "olivera", "olivo", "almacen", "tiraluzlarga"})
    if terms & {"casa", "interior", "dentro"}:
        expanded.update({"casa", "interior", "dentro", "salon", "comedor", "oficina", "sonar"})
    return expanded


def security_row_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "entity_id", "name", "label", "friendly_name", "area", "area_name",
        "device", "device_name", "kind", "type", "aliases",
    ):
        value = row.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value is not None:
            parts.append(str(value))
    return fold_text(" ".join(parts))


def security_row_label(row: dict[str, Any]) -> str:
    for key in ("name", "label", "friendly_name", "entity_name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    entity_id = str(row.get("entity_id") or "").strip()
    return entity_id or "sensor seguridad"


def security_row_count(row: dict[str, Any], count_key: str) -> int:
    for key in (count_key, count_key.replace("count_", "total_"), count_key.removeprefix("count_")):
        try:
            return int(float(row.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0


async def ha_get_security_activity(context: Any, args: dict[str, Any]) -> str:
    area = str(args.get("area") or args.get("query") or "").strip()
    window, count_key, window_label = security_activity_window(str(args.get("window") or args.get("period") or "24h"))
    include_zero = bool(args.get("include_zero", False))
    state = await fetch_ha_state(context, "sensor.camera_security_test_count")
    attrs = state.get("attributes") or {}
    rows_raw = attrs.get("sensor_rows") or attrs.get("rows") or attrs.get("sensors") or []
    if not isinstance(rows_raw, list):
        rows_raw = []
    terms = security_area_terms(area)
    rows: list[dict[str, Any]] = []
    for raw in rows_raw:
        if not isinstance(raw, dict):
            continue
        haystack = security_row_text(raw)
        if terms and not any(term in haystack for term in terms):
            continue
        count = security_row_count(raw, count_key)
        if count <= 0 and not include_zero:
            continue
        row = dict(raw)
        row["label"] = security_row_label(raw)
        row["activity_count"] = count
        rows.append(row)
    rows.sort(key=lambda row: (-int(row.get("activity_count") or 0), security_row_label(row)))

    active_labels = [f"{security_row_label(row)} ({int(row.get('activity_count') or 0)})" for row in rows if int(row.get("activity_count") or 0) > 0]
    total = sum(int(row.get("activity_count") or 0) for row in rows)
    scope = f" en {area}" if area else ""
    if active_labels:
        recommended = f"Si, ha habido movimiento{scope} en {window_label}: " + ", ".join(active_labels[:8]) + "."
        if len(active_labels) > 8:
            recommended += f" Y {len(active_labels) - 8} sensores mas."
    else:
        recommended = f"No veo actividad de seguridad{scope} en {window_label}."
    return compact_json(
        {
            "entity_id": "sensor.camera_security_test_count",
            "state": state.get("state"),
            "last_updated": state.get("last_updated"),
            "window": window,
            "window_label": window_label,
            "area": area or None,
            "matched_sensors": len(rows),
            "total_activations": total,
            "rows": rows[:50],
            "recommended_answer": recommended,
            "note": "Usa recommended_answer como base. Cada etiqueta lleva entre parentesis el numero de activaciones en la ventana consultada.",
        },
        max_chars=24000,
    )


async def ha_get_tts_media_players(context: Any, args: dict[str, Any]) -> str:
    query = str(args.get("query", "") or "").strip().lower()
    limit = max(1, min(int(args.get("limit", 100) or 100), 200))
    terms = [term for term in re.split(r"\W+", query) if term]
    candidates_from_ha = await get_tts_media_player_candidates(context)

    candidates: list[dict[str, Any]] = []
    excluded_not_audible = 0
    for state in candidates_from_ha:
        if not is_audible_tts_candidate(state):
            excluded_not_audible += 1
            continue
        entity_id = str(state.get("entity_id", ""))
        current_state = str(state.get("state", "") or "").lower()
        friendly_name = state.get("friendly_name")
        haystack = " ".join(
            str(part).lower()
            for part in (
                entity_id,
                friendly_name or "",
                current_state,
            )
        )
        if terms and not all(term in haystack for term in terms):
            continue
        candidates.append(
            {
                "entity_id": entity_id,
                "state": current_state,
                "friendly_name": friendly_name,
                "volume_level": state.get("volume_level"),
                "is_volume_muted": state.get("is_volume_muted"),
                "available_for_tts": True,
                "note": "off tambien es valido para TTS; Home Assistant puede activar el media_player al reproducir.",
            }
        )
        if len(candidates) >= limit:
            break
    return compact_json(
        {
            "candidates": candidates,
            "count": len(candidates),
            "excluded_not_audible": excluded_not_audible,
            "rule": "Para TTS usa estos media_player como opciones audibles. Si el usuario indico destino claro, habla sin confirmacion extra. Tras llamar TTS, di que el mensaje fue enviado; no afirmes que se oyo o se reprodujo.",
        },
        max_chars=20000,
    )


async def ha_get_services(context: Any, args: dict[str, Any]) -> str:
    domain = str(args.get("domain", "") or "").strip().lower()
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/services", headers=ha_headers(context))
        response.raise_for_status()
        services = response.json()
    if domain:
        services = [item for item in services if str(item.get("domain", "")).lower() == domain]
    return compact_json(services, max_chars=24000)


async def call_ha_service(context: Any, domain: str, service: str, payload: dict[str, Any], *, timeout: int = 30) -> Any:
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=timeout) as http:
        response = await http.post(
            f"{ha_base_url(context)}/api/services/{domain}/{service}",
            headers=ha_headers(context),
            json=payload,
        )
        response.raise_for_status()
        return response.json() if response.content else {"ok": True}


def payload_entity_ids(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("entity_id")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


AC_POWER_SENSOR = "sensor.powcasa_enchufes_powcasa_enchufes_power"
AC_POWER_DELTA_THRESHOLD_W = 300.0
POWER_VERIFIED_AC_CONTROLS = {"input_boolean.brokton_ac_dp1_switch"}


def numeric_state_value(state: dict[str, Any]) -> float | None:
    try:
        return float(state.get("state"))
    except (TypeError, ValueError):
        return None


def ac_power_expected_direction(domain: str, service: str, payload: dict[str, Any]) -> str | None:
    if domain != "input_boolean":
        return None
    entity_ids = set(payload_entity_ids(payload))
    if not (entity_ids & POWER_VERIFIED_AC_CONTROLS):
        return None
    if service == "turn_on":
        return "increase"
    if service == "turn_off":
        return "decrease"
    return None


async def verify_ac_power_delta(
    context: Any,
    *,
    direction: str,
    before_power: float | None,
    attempts: int = 12,
    delay_seconds: float = 5.0,
) -> dict[str, Any]:
    readings: list[dict[str, Any]] = []
    if before_power is None:
        return {
            "verified": False,
            "reason": "baseline_power_unavailable",
            "sensor": AC_POWER_SENSOR,
            "threshold_w": AC_POWER_DELTA_THRESHOLD_W,
        }
    for attempt in range(1, max(1, attempts) + 1):
        state = await fetch_ha_state(context, AC_POWER_SENSOR)
        current_power = numeric_state_value(state)
        delta = current_power - before_power if current_power is not None else None
        reading = {
            "attempt": attempt,
            "power_w": current_power,
            "delta_w": delta,
            "last_updated": state.get("last_updated"),
        }
        readings.append(reading)
        if delta is not None:
            if direction == "increase" and delta >= AC_POWER_DELTA_THRESHOLD_W:
                return {
                    "verified": True,
                    "direction": direction,
                    "sensor": AC_POWER_SENSOR,
                    "baseline_w": before_power,
                    "threshold_w": AC_POWER_DELTA_THRESHOLD_W,
                    "readings": readings,
                }
            if direction == "decrease" and delta <= -AC_POWER_DELTA_THRESHOLD_W:
                return {
                    "verified": True,
                    "direction": direction,
                    "sensor": AC_POWER_SENSOR,
                    "baseline_w": before_power,
                    "threshold_w": AC_POWER_DELTA_THRESHOLD_W,
                    "readings": readings,
                }
        if attempt < attempts:
            await asyncio.sleep(delay_seconds)
    return {
        "verified": False,
        "direction": direction,
        "sensor": AC_POWER_SENSOR,
        "baseline_w": before_power,
        "threshold_w": AC_POWER_DELTA_THRESHOLD_W,
        "readings": readings,
    }


async def verify_service_state(
    context: Any,
    *,
    domain: str,
    service: str,
    payload: dict[str, Any],
    attempts: int | None = None,
    delay_seconds: float | None = None,
) -> dict[str, Any] | None:
    expected_by_service = {"turn_on": "on", "turn_off": "off"}
    expected = expected_by_service.get(service)
    if not expected:
        return None
    entity_ids = [entity_id for entity_id in payload_entity_ids(payload) if entity_id.startswith(f"{domain}.")]
    if not entity_ids:
        return None

    attempts = max(1, int(attempts if attempts is not None else getattr(context, "service_verify_attempts", 8)))
    delay_seconds = max(0.0, float(delay_seconds if delay_seconds is not None else getattr(context, "service_verify_delay", 0.5)))
    last_states: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        last_states = []
        for entity_id in entity_ids:
            state = await fetch_ha_state(context, entity_id)
            last_states.append(
                {
                    "entity_id": entity_id,
                    "state": state.get("state"),
                    "last_changed": state.get("last_changed"),
                    "last_updated": state.get("last_updated"),
                }
            )
        if last_states and all(str(item.get("state") or "").lower() == expected for item in last_states):
            return {"verified": True, "expected_state": expected, "attempt": attempt, "states": last_states}
        if attempt < attempts:
            await asyncio.sleep(delay_seconds)
    return {"verified": False, "expected_state": expected, "attempt": attempts, "states": last_states}


async def ha_send_mobile_alert(context: Any, args: dict[str, Any]) -> str:
    message = str(args.get("message", "") or "Han llamado a la puerta").strip()
    title = str(args.get("title", "") or "").strip()
    notify = str(args.get("notify", "") or "notify/mobile_app_sm_a566b").strip()
    volume = max(0, min(int(args.get("volume", 100) or 100), 100))
    media_stream = str(args.get("media_stream", "") or "alarm_stream").strip()
    speak = bool(args.get("speak", True))
    critical = bool(args.get("critical", True))
    if "/" in notify:
        domain, service = notify.split("/", 1)
    elif "." in notify:
        domain, service = notify.split(".", 1)
    else:
        domain, service = "notify", notify
    if domain != "notify" or not service:
        raise ValueError("notify debe ser un servicio notify, por ejemplo notify/mobile_app_sm_a566b")

    volume_payload = {
        "message": "command_volume_level",
        "data": {
            "media_stream": media_stream,
            "command": volume,
        },
    }
    notification_data: dict[str, Any] = {
        "ttl": 0,
        "priority": "high" if critical else "normal",
    }
    if critical:
        notification_data.update(
            {
                "channel": media_stream,
                "importance": "high",
                "persistent": True,
                "sound": "default",
                "push": {
                    "sound": {
                        "name": "default",
                        "critical": 1,
                        "volume": 1.0,
                    }
                },
            }
        )
    custom_data = args.get("data")
    if isinstance(custom_data, dict):
        notification_data.update(custom_data)

    message_payload: dict[str, Any] = {
        "message": message,
        "data": notification_data,
    }
    if title:
        message_payload["title"] = title

    volume_response = await call_ha_service(context, domain, service, volume_payload)
    responses = [volume_response]
    tts_payload: dict[str, Any] | None = None
    if speak:
        tts_payload = {
            "message": "TTS",
            "data": {
                "ttl": 0,
                "priority": "high",
                "media_stream": media_stream,
                "tts_text": message,
            },
        }
        responses.append(await call_ha_service(context, domain, service, tts_payload))
    message_response = await call_ha_service(context, domain, service, message_payload)
    responses.append(message_response)
    return compact_json(
        {
            "called": True,
            "notify": f"{domain}/{service}",
            "speak": speak,
            "critical": critical,
            "volume_command": volume_payload,
            "tts_payload": tts_payload,
            "message_payload": message_payload,
            "responses": responses,
            "rule": "Alerta enviada al movil con TTS y sonido critico cuando estan habilitados. La API confirma las llamadas al servicio notify, no que el usuario las haya oido o leido.",
        },
        max_chars=20000,
    )


async def ha_call_service(context: Any, args: dict[str, Any]) -> str:
    domain = str(args.get("domain", "") or "").strip()
    service = str(args.get("service", "") or "").strip()
    if not domain or not service:
        raise ValueError("domain y service son obligatorios")
    confirm = bool(args.get("confirm", False))
    if (
        getattr(context, "require_action_confirmation", True)
        and not confirm
        and not is_tts_service(domain, service)
        and not is_notify_service(domain, service)
    ):
        raise PermissionError("Accion rechazada: falta confirm=true tras confirmacion explicita del usuario.")
    payload: dict[str, Any] = {}
    service_data = args.get("service_data")
    target = args.get("target")
    if isinstance(service_data, dict):
        payload.update(service_data)
    if isinstance(target, dict):
        payload["target"] = target
    if is_tts_service(domain, service):
        payload.setdefault("cache", False)
        payload.setdefault("language", "es")
        await resolve_tts_payload_entities(context, payload)
        entity_warnings: list[str] = []
    else:
        entity_warnings = await resolve_payload_entity_ids(context, payload, domain=domain if domain != "homeassistant" else "")
    validate_tts_service_call(domain, service, payload, confirm)
    power_direction = ac_power_expected_direction(domain, service, payload)
    before_ac_power = None
    if power_direction:
        before_ac_power = numeric_state_value(await fetch_ha_state(context, AC_POWER_SENSOR))
    data = await call_ha_service(context, domain, service, payload)
    verification = None
    power_verification = None
    if not is_tts_service(domain, service) and not is_notify_service(domain, service):
        verification = await verify_service_state(context, domain=domain, service=service, payload=payload)
        if power_direction:
            power_verification = await verify_ac_power_delta(
                context,
                direction=power_direction,
                before_power=before_ac_power,
            )
    return compact_json(
        {
            "called": True,
            "domain": domain,
            "service": service,
            "warnings": entity_warnings,
            "response": data,
            "verification": verification,
            "power_verification": power_verification,
        },
        max_chars=20000,
    )


async def ha_press_entity_interval(context: Any, args: dict[str, Any]) -> str:
    entity_id = str(args.get("entity_id", "") or "").strip()
    query = str(args.get("query", "") or "").strip()
    domain_hint = str(args.get("domain", "") or "").strip().lower()
    try:
        duration = float(args.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("duration_seconds debe ser numerico")
    if duration <= 0:
        raise ValueError("duration_seconds debe ser mayor que 0")
    if duration > 600:
        raise ValueError("duration_seconds no puede superar 600 segundos")
    confirm = bool(args.get("confirm", False))
    if getattr(context, "require_action_confirmation", True) and not confirm:
        raise PermissionError("Accion rechazada: falta confirm=true tras confirmacion explicita del usuario.")

    resolved, warnings = await resolve_entity_reference(
        context,
        entity_id=entity_id,
        query=query,
        domain=domain_hint,
        include_unavailable=True,
    )
    if not resolved or "." not in resolved:
        raise ValueError("No pude resolver la entidad a pulsar")
    resolved_domain = resolved.split(".", 1)[0]
    if resolved_domain in {"button", "input_button"}:
        service_domain = resolved_domain
        service = "press"
    elif resolved_domain == "switch":
        service_domain = "switch"
        service = "toggle"
    else:
        raise ValueError(f"Entidad no soportada para pulso temporizado: {resolved}")

    dedupe_key = (resolved, duration)
    now = time.monotonic()
    previous = LAST_INTERVAL_ACTIONS.get(dedupe_key)
    if previous is not None and now - previous < INTERVAL_ACTION_DEDUPE_SECONDS:
        raise PermissionError(
            f"Accion duplicada bloqueada por seguridad: {resolved} durante {duration:g}s ya se ejecuto hace {now - previous:.1f}s."
        )
    LAST_INTERVAL_ACTIONS[dedupe_key] = now

    payload = {"entity_id": resolved}
    first = await call_ha_service(context, service_domain, service, payload)
    await asyncio.sleep(duration)
    second = await call_ha_service(context, service_domain, service, payload)
    return compact_json(
        {
            "called": True,
            "entity_id": resolved,
            "service_sequence": [f"{service_domain}.{service}", f"sleep:{duration:g}s", f"{service_domain}.{service}"],
            "warnings": warnings,
            "responses": [first, second],
            "rule": "Accion pulsable temporizada: se pulsa/togglea una vez para iniciar y otra para finalizar.",
        },
        max_chars=20000,
    )


async def ha_render_template(context: Any, args: dict[str, Any]) -> str:
    template = str(args.get("template", "") or "")
    if not template.strip():
        raise ValueError("template es obligatorio")
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(
            f"{ha_base_url(context)}/api/template",
            headers=ha_headers(context),
            json={"template": template},
        )
        response.raise_for_status()
        return compact_json({"result": response.text}, max_chars=12000)


async def ha_get_events(context: Any, args: dict[str, Any]) -> str:
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/events", headers=ha_headers(context))
        response.raise_for_status()
        return compact_json(response.json(), max_chars=16000)


async def ha_get_error_log(context: Any, args: dict[str, Any]) -> str:
    max_chars = max(1000, min(int(args.get("max_chars", 12000) or 12000), 50000))
    httpx = httpx_module(context)
    headers = ha_headers(context)
    headers["Accept"] = "text/plain, application/json"
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/error_log", headers=headers)
        response.raise_for_status()
        text = response.text
    return compact_json({"truncated": len(text) > max_chars, "text": text[-max_chars:]}, max_chars=max_chars + 1000)


async def ha_get_history(context: Any, args: dict[str, Any]) -> str:
    start_time = str(args.get("start_time", "") or "").strip()
    if not start_time:
        raise ValueError("start_time es obligatorio")
    end_time = str(args.get("end_time", "") or "").strip()
    entity_id = str(args.get("entity_id", "") or "").strip()
    entity_ids: list[str]
    if entity_id:
        resolved, _warnings = await resolve_entity_reference(
            context,
            entity_id=entity_id,
            domain=str(args.get("domain", "") or "").strip().lower(),
        )
        entity_ids = [resolved or entity_id]
    else:
        entity_ids = await resolve_history_entities(context, args)
    if not entity_ids:
        return compact_json({"entities": [], "message": "No se encontraron entidades candidatas para consultar historico."})

    httpx = httpx_module(context)
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as http:
        for candidate in entity_ids:
            params: dict[str, str] = {
                "minimal_response": "1",
                "no_attributes": "1",
                "filter_entity_id": candidate,
            }
            if end_time:
                params["end_time"] = end_time
            response = await http.get(
                f"{ha_base_url(context)}/api/history/period/{start_time}",
                headers=ha_headers(context),
                params=params,
            )
            response.raise_for_status()
            history = response.json()
            points = flatten_history_points(history)
            useful_points = useful_history_points(points)
            rows.append(
                {
                    "entity_id": candidate,
                    "points": len(points),
                    "useful_points": len(useful_points),
                    "first": useful_points[0] if useful_points else points[0] if points else None,
                    "last": useful_points[-1] if useful_points else points[-1] if points else None,
                    "sample": useful_points[: min(5, len(useful_points))] or points[: min(5, len(points))],
                }
            )
    return compact_json(
        {
            "start_time": start_time,
            "end_time": end_time or None,
            "entities_checked": len(entity_ids),
            "entities_with_data": sum(1 for row in rows if row["useful_points"]),
            "results": rows,
        },
        max_chars=24000,
    )



async def ha_get_history_around_time(context: Any, args: dict[str, Any]) -> str:
    entity_id, warnings = await resolve_entity_for_history_around_time(context, args)
    if not entity_id:
        return compact_json({"message": "No se encontro entidad candidata para consultar historico alrededor de una hora."})

    days = max(1, min(int(args.get("days", 7) or 7), 31))
    local_time_text = str(args.get("local_time", "10:00") or "10:00")
    window_minutes = max(1, min(int(args.get("window_minutes", 30) or 30), 240))
    timezone_name = str(args.get("timezone", "") or "Europe/Madrid")
    timezone = ZoneInfo(timezone_name)
    target_time = parse_local_time(local_time_text)

    end_date_text = str(args.get("end_date", "") or "").strip()
    if end_date_text:
        end_date = dt.date.fromisoformat(end_date_text)
    else:
        end_date = dt.datetime.now(timezone).date()
    start_date = end_date - dt.timedelta(days=days - 1)

    httpx = httpx_module(context)
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as http:
        for offset in range(days):
            day = start_date + dt.timedelta(days=offset)
            target = dt.datetime.combine(day, target_time, tzinfo=timezone)
            start = target - dt.timedelta(minutes=window_minutes)
            end = target + dt.timedelta(minutes=window_minutes)
            params = {
                "minimal_response": "1",
                "no_attributes": "1",
                "filter_entity_id": entity_id,
                "end_time": end.isoformat(),
            }
            response = await http.get(
                f"{ha_base_url(context)}/api/history/period/{start.isoformat()}",
                headers=ha_headers(context),
                params=params,
            )
            response.raise_for_status()
            points = flatten_history_points(response.json())
            nearest = choose_nearest_history_point(points, target)
            nearest_time = (
                parse_ha_datetime(str(nearest.get("last_updated") or nearest.get("last_changed") or ""))
                if nearest
                else None
            )
            rows.append(
                {
                    "date": day.isoformat(),
                    "target_local": target.isoformat(),
                    "window_start_local": start.isoformat(),
                    "window_end_local": end.isoformat(),
                    "points": len(points),
                    "nearest": nearest,
                    "nearest_local": nearest_time.astimezone(timezone).isoformat() if nearest_time else None,
                    "minutes_from_target": (
                        round(abs((nearest_time - target.astimezone(nearest_time.tzinfo)).total_seconds()) / 60, 2)
                        if nearest_time
                        else None
                    ),
                }
            )

    return compact_json(
        {
            "entity_id": entity_id,
            "timezone": timezone_name,
            "local_time": local_time_text,
            "window_minutes": window_minutes,
            "days": days,
            "warnings": warnings,
            "days_with_data": sum(1 for row in rows if row["nearest"]),
            "results": rows,
        },
        max_chars=24000,
    )


async def resolve_history_entities(context: Any, args: dict[str, Any]) -> list[str]:
    query = str(args.get("query", "") or "").strip().lower()
    domain = str(args.get("domain", "sensor") or "sensor").strip().lower()
    device_class = str(args.get("device_class", "") or "").strip().lower()
    limit = max(1, min(int(args.get("limit", 12) or 12), 50))
    states_raw = await ha_get_states(context, {"query": query, "domain": domain, "limit": 500})
    states = json.loads(states_raw)
    candidates: list[str] = []
    for state in states:
        if device_class and str(state.get("device_class") or "").lower() != device_class:
            continue
        entity_id = str(state.get("entity_id") or "")
        if entity_id and entity_id not in candidates:
            candidates.append(entity_id)
        if len(candidates) >= limit:
            break
    return candidates


def useful_history_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [point for point in points if str(point.get("state", "")).lower() not in {"unknown", "unavailable"}]


def flatten_history_points(history: Any) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    if not isinstance(history, list):
        return points
    for entity_history in history:
        if not isinstance(entity_history, list):
            continue
        for item in entity_history:
            if not isinstance(item, dict):
                continue
            points.append(
                {
                    "state": item.get("state"),
                    "last_changed": item.get("last_changed"),
                    "last_updated": item.get("last_updated"),
                }
            )
    return points


HISTORY_AGGREGATIONS = {"min", "max", "mean", "first", "last", "delta"}
HISTORY_GROUPS = {"hour", "day", "week"}
WEEKDAY_NAMES_ES = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")


def numeric_history_group(local_time: dt.datetime, group_by: str) -> tuple[str, dt.datetime]:
    if group_by == "hour":
        start = local_time.replace(minute=0, second=0, microsecond=0)
        return start.isoformat(timespec="seconds"), start
    if group_by == "week":
        start = (local_time - dt.timedelta(days=local_time.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        iso_year, iso_week, _ = start.isocalendar()
        return f"{iso_year}-W{iso_week:02d}", start
    start = local_time.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.date().isoformat(), start


def aggregate_numeric_values(values: list[float], aggregation: str) -> float:
    if aggregation == "min":
        return min(values)
    if aggregation == "max":
        return max(values)
    if aggregation == "mean":
        return sum(values) / len(values)
    if aggregation == "first":
        return values[0]
    if aggregation == "last":
        return values[-1]
    return values[-1] - values[0]


def rounded_history_value(value: float) -> float:
    return round(value, 6)


async def ha_aggregate_numeric_history(context: Any, args: dict[str, Any]) -> str:
    start_time = normalize_history_iso(str(args.get("start_time", "") or "").strip())
    if not start_time:
        raise ValueError("start_time es obligatorio")
    end_time = normalize_history_iso(str(args.get("end_time", "") or "").strip())
    group_by = str(args.get("group_by", "day") or "day").strip().lower()
    aggregation = str(args.get("aggregation", "max") or "max").strip().lower()
    if group_by not in HISTORY_GROUPS:
        raise ValueError(f"group_by debe ser uno de: {', '.join(sorted(HISTORY_GROUPS))}")
    if aggregation not in HISTORY_AGGREGATIONS:
        raise ValueError(f"aggregation debe ser una de: {', '.join(sorted(HISTORY_AGGREGATIONS))}")
    timezone_name = str(args.get("timezone", "Europe/Madrid") or "Europe/Madrid").strip()
    timezone = ZoneInfo(timezone_name)
    exclude_start_state = bool(args.get("exclude_start_state", True))
    start_at = parse_ha_datetime(start_time)
    end_at = parse_ha_datetime(end_time) if end_time else None
    if start_at is None:
        raise ValueError("start_time no es una fecha ISO 8601 valida")
    if end_at is not None and end_at <= start_at:
        raise ValueError("end_time debe ser posterior a start_time")

    entity_id = str(args.get("entity_id", "") or "").strip()
    if entity_id:
        resolved, _warnings = await resolve_entity_reference(
            context,
            entity_id=entity_id,
            domain=str(args.get("domain", "") or "").strip().lower(),
        )
        entity_ids = [resolved or entity_id]
    else:
        entity_ids = await resolve_history_entities(context, args)
    if not entity_ids:
        return compact_json({"entities": [], "message": "No se encontraron sensores numericos para agregar."})

    httpx = httpx_module(context)
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=45) as http:
        for candidate in entity_ids:
            params: dict[str, str] = {
                "minimal_response": "1",
                "no_attributes": "1",
                "filter_entity_id": candidate,
            }
            if end_time:
                params["end_time"] = end_time
            state_response = await http.get(
                f"{ha_base_url(context)}/api/states/{candidate}",
                headers=ha_headers(context),
            )
            state_response.raise_for_status()
            current_state = state_response.json()
            attributes = current_state.get("attributes") if isinstance(current_state, dict) else {}
            response = await http.get(
                f"{ha_base_url(context)}/api/history/period/{start_time}",
                headers=ha_headers(context),
                params=params,
            )
            response.raise_for_status()
            groups: dict[str, dict[str, Any]] = {}
            ignored_non_numeric = 0
            ignored_start_state = 0
            for point in useful_history_points(flatten_history_points(response.json())):
                when = parse_ha_datetime(str(point.get("last_updated") or point.get("last_changed") or ""))
                if when is None or when < start_at or (end_at is not None and when >= end_at):
                    continue
                if exclude_start_state and abs((when - start_at).total_seconds()) < 0.001:
                    ignored_start_state += 1
                    continue
                try:
                    value = float(point.get("state"))
                except (TypeError, ValueError):
                    ignored_non_numeric += 1
                    continue
                if not math.isfinite(value):
                    ignored_non_numeric += 1
                    continue
                local_time = when.astimezone(timezone)
                key, period_start = numeric_history_group(local_time, group_by)
                group = groups.setdefault(
                    key,
                    {"period": key, "period_start_local": period_start.isoformat(timespec="seconds"), "samples": []},
                )
                group["samples"].append((when, value))

            periods: list[dict[str, Any]] = []
            for key in sorted(groups):
                group = groups[key]
                samples = sorted(group.pop("samples"), key=lambda item: item[0])
                values = [value for _, value in samples]
                selected = aggregate_numeric_values(values, aggregation)
                row = {
                    **group,
                    "samples": len(values),
                    "first": rounded_history_value(values[0]),
                    "last": rounded_history_value(values[-1]),
                    "min": rounded_history_value(min(values)),
                    "max": rounded_history_value(max(values)),
                    "mean": rounded_history_value(sum(values) / len(values)),
                    "delta": rounded_history_value(values[-1] - values[0]),
                    "value": rounded_history_value(selected),
                }
                if group_by == "day":
                    day = dt.date.fromisoformat(key)
                    row["weekday"] = day.isoweekday()
                    row["weekday_name"] = WEEKDAY_NAMES_ES[day.weekday()]
                periods.append(row)
            highest = max(periods, key=lambda row: row["value"]) if periods else None
            lowest = min(periods, key=lambda row: row["value"]) if periods else None
            results.append(
                {
                    "entity_id": candidate,
                    "friendly_name": attributes.get("friendly_name") if isinstance(attributes, dict) else None,
                    "unit": attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None,
                    "device_class": attributes.get("device_class") if isinstance(attributes, dict) else None,
                    "periods_with_data": len(periods),
                    "ignored_non_numeric": ignored_non_numeric,
                    "ignored_start_state": ignored_start_state,
                    "highest": highest,
                    "lowest": lowest,
                    "periods": periods,
                }
            )
    return compact_json(
        {
            "start_time": start_time,
            "end_time": end_time or None,
            "timezone": timezone_name,
            "group_by": group_by,
            "aggregation": aggregation,
            "exclude_start_state": exclude_start_state,
            "entities_checked": len(entity_ids),
            "results": results,
        },
        max_chars=30000,
    )


async def ha_get_long_term_statistics(context: Any, args: dict[str, Any]) -> str:
    entity_id = str(args.get("entity_id", "") or "").strip()
    if not entity_id:
        raise ValueError("entity_id es obligatorio")
    start_time = normalize_history_iso(str(args.get("start_time", "") or "").strip())
    end_time = normalize_history_iso(str(args.get("end_time", "") or "").strip())
    if not start_time or not end_time:
        raise ValueError("start_time y end_time son obligatorios")
    start_at = parse_ha_datetime(start_time)
    end_at = parse_ha_datetime(end_time)
    if start_at is None or end_at is None:
        raise ValueError("start_time y end_time deben ser fechas ISO 8601 validas")
    if end_at <= start_at:
        raise ValueError("end_time debe ser posterior a start_time")

    result = await ha_websocket_command(
        context,
        {
            "type": "recorder/statistics_during_period",
            "start_time": start_time,
            "end_time": end_time,
            "statistic_ids": [entity_id],
            "period": "hour",
            "types": ["change", "sum", "state"],
        },
    )
    rows = result.get(entity_id, []) if isinstance(result, dict) else []
    changes = [float(row["change"]) for row in rows if row.get("change") is not None]
    sums = [float(row["sum"]) for row in rows if row.get("sum") is not None]
    value = sum(changes) if changes else (sums[-1] - sums[0] if len(sums) >= 2 else None)
    return compact_json(
        {
            "entity_id": entity_id,
            "start_time": start_time,
            "end_time": end_time,
            "period": "hour",
            "statistics": rows,
            "samples": len(rows),
            "value": rounded_history_value(value) if value is not None else None,
            "calculation": "sum(change)" if changes else "last(sum)-first(sum)" if len(sums) >= 2 else None,
        },
        max_chars=30000,
    )



async def ha_get_pvpc_cheapest_hours(context: Any, args: dict[str, Any]) -> str:
    sensor_entity_id = str(args.get("sensor_entity_id", "sensor.pvpc_dh") or "sensor.pvpc_dh").strip()
    limit = max(1, min(int(args.get("limit", 5) or 5), 24))
    include_all_hours = bool(args.get("include_all_hours", False))
    timezone_name = str(args.get("timezone", "Europe/Madrid") or "Europe/Madrid").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("Europe/Madrid")
        timezone_name = "Europe/Madrid"

    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.get(
            f"{ha_base_url(context)}/api/states/{sensor_entity_id}",
            headers=ha_headers(context),
        )
        response.raise_for_status()
        state = response.json()

    attrs = state.get("attributes") or {}
    rows: list[dict[str, Any]] = []
    for hour in range(24):
        value = attrs.get(f"price_{hour:02d}h")
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "hour": hour,
                "start": f"{hour:02d}:00",
                "end": f"{(hour + 1) % 24:02d}:00",
                "range": f"{hour:02d}:00-{(hour + 1) % 24:02d}:00",
                "eur_kwh": round(price, 5),
                "cent_kwh": round(price * 100, 2),
            }
        )

    if not rows:
        return compact_json(
            {
                "sensor_entity_id": sensor_entity_id,
                "message": "No hay atributos price_00h..price_23h disponibles en el sensor PVPC.",
            }
        )

    now_local = dt.datetime.now(timezone)
    current_hour = now_local.hour
    sorted_rows = sorted(rows, key=lambda item: (item["eur_kwh"], item["hour"]))
    future_rows = [row for row in rows if row["hour"] >= current_hour]
    sorted_future = sorted(future_rows, key=lambda item: (item["eur_kwh"], item["hour"]))

    min_price = attrs.get("min_price")
    max_price = attrs.get("max_price")
    try:
        min_price_float = float(min_price)
    except (TypeError, ValueError):
        min_price_float = sorted_rows[0]["eur_kwh"]
    try:
        max_price_float = float(max_price)
    except (TypeError, ValueError):
        max_price_float = sorted_rows[-1]["eur_kwh"]

    selected = sorted_rows[:limit]
    recommended_lines = [
        "La luz/consumo/kWh mas barato hoy es:",
        *[
            f"- {row['range']}: {row['eur_kwh']:.5f} EUR/kWh ({row['cent_kwh']:.2f} cent/kWh)"
            for row in selected
        ],
        f"La franja minima es {sorted_rows[0]['range']} con {sorted_rows[0]['cent_kwh']:.2f} cent/kWh.",
        f"La mas cara es {sorted_rows[-1]['range']} con {sorted_rows[-1]['cent_kwh']:.2f} cent/kWh.",
    ]
    if sorted_future:
        recommended_lines.append(
            f"Desde ahora, la proxima/mejor franja es {sorted_future[0]['range']} con {sorted_future[0]['cent_kwh']:.2f} cent/kWh."
        )

    return compact_json(
        {
            "sensor_entity_id": sensor_entity_id,
            "meaning": "Precio PVPC de electricidad. En estas consultas 'luz', 'consumo' y 'kWh' se refieren a precio electrico, no a luces fisicas.",
            "recommended_answer": "\n".join(recommended_lines),
            "timezone": timezone_name,
            "now_local": now_local.isoformat(timespec="seconds"),
            "current_price": {
                "eur_kwh": round(float(state.get("state")), 5) if str(state.get("state", "")).replace(".", "", 1).isdigit() else state.get("state"),
                "cent_kwh": round(float(state.get("state")) * 100, 2) if str(state.get("state", "")).replace(".", "", 1).isdigit() else None,
                "period": attrs.get("period"),
            },
            "cheapest_hour": sorted_rows[0],
            "cheapest_hours": sorted_rows[:limit],
            "cheapest_future_hour": sorted_future[0] if sorted_future else None,
            "cheapest_future_hours": sorted_future[:limit],
            "min_price": {"eur_kwh": round(min_price_float, 5), "cent_kwh": round(min_price_float * 100, 2), "hour": attrs.get("min_price_at")},
            "max_price": {"eur_kwh": round(max_price_float, 5), "cent_kwh": round(max_price_float * 100, 2), "hour": attrs.get("max_price_at")},
            "next_best_at": attrs.get("next_best_at"),
            "all_hours": rows if include_all_hours else None,
        },
        max_chars=30000,
    )


async def ha_plan_ac_pvpc_budget(context: Any, args: dict[str, Any]) -> str:
    budget_eur = max(0.01, float(args.get("budget_eur", 1.0) or 1.0))
    sensor_entity_id = str(args.get("sensor_entity_id", "sensor.pvpc_dh") or "sensor.pvpc_dh").strip()
    climate_entity_id = str(args.get("climate_entity_id", "input_boolean.brokton_ac_dp1_switch") or "input_boolean.brokton_ac_dp1_switch").strip()
    power_sensor_entity_id = str(
        args.get("power_sensor_entity_id", AC_POWER_SENSOR) or AC_POWER_SENSOR
    ).strip()
    ac_power_kw = max(0.1, float(args.get("ac_power_kw", 1.2) or 1.2))
    timezone_name = str(args.get("timezone", "Europe/Madrid") or "Europe/Madrid").strip()
    start_hour = args.get("start_hour")
    end_hour = args.get("end_hour")
    only_future = bool(args.get("only_future", True))
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("Europe/Madrid")
        timezone_name = "Europe/Madrid"

    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.get(
            f"{ha_base_url(context)}/api/states/{sensor_entity_id}",
            headers=ha_headers(context),
        )
        response.raise_for_status()
        pvpc_state = response.json()
        power_response = await http.get(
            f"{ha_base_url(context)}/api/states/{power_sensor_entity_id}",
            headers=ha_headers(context),
        )
        current_power_state = power_response.json() if power_response.status_code == 200 else {}

    attrs = pvpc_state.get("attributes") or {}
    now_local = dt.datetime.now(timezone)
    current_hour = now_local.hour
    try:
        start_hour_int = int(start_hour) if start_hour is not None else 0
    except (TypeError, ValueError):
        start_hour_int = 0
    try:
        end_hour_int = int(end_hour) if end_hour is not None else 24
    except (TypeError, ValueError):
        end_hour_int = 24
    start_hour_int = max(0, min(23, start_hour_int))
    end_hour_int = max(1, min(24, end_hour_int))

    rows: list[dict[str, Any]] = []
    for hour in range(24):
        if not (start_hour_int <= hour < end_hour_int):
            continue
        if only_future and hour < current_hour:
            continue
        value = attrs.get(f"price_{hour:02d}h")
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "hour": hour,
                "range": f"{hour:02d}:00-{(hour + 1) % 24:02d}:00",
                "eur_kwh": round(price, 5),
                "cent_kwh": round(price * 100, 2),
            }
        )

    remaining = budget_eur
    selected: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item["eur_kwh"], item["hour"])):
        hourly_cost = ac_power_kw * float(row["eur_kwh"])
        if hourly_cost <= 0:
            continue
        duration_hours = min(1.0, remaining / hourly_cost)
        if duration_hours < (1 / 60):
            continue
        cost = duration_hours * hourly_cost
        minutes = int(round(duration_hours * 60))
        selected.append(
            {
                **row,
                "duration_minutes": minutes,
                "estimated_kwh": round(ac_power_kw * (minutes / 60), 3),
                "estimated_cost_eur": round(ac_power_kw * (minutes / 60) * float(row["eur_kwh"]), 4),
                "turn_on_at": f"{row['hour']:02d}:00",
                "turn_off_at": f"{row['hour']:02d}:{minutes:02d}" if minutes < 60 else f"{(row['hour'] + 1) % 24:02d}:00",
            }
        )
        remaining -= cost
        if remaining <= 0.005:
            break

    total_cost = sum(float(item["estimated_cost_eur"]) for item in selected)
    total_kwh = sum(float(item["estimated_kwh"]) for item in selected)
    current_power_w = numeric_state_value(current_power_state)
    explanation_lines = [
        f"Plan candidato para gastar hasta {budget_eur:.2f} EUR en AC hoy.",
        f"Premisa: {climate_entity_id}, potencia media estimada {ac_power_kw:.2f} kW, verificacion con {power_sensor_entity_id} y umbral 300 W.",
    ]
    if selected:
        selected_chronological = sorted(selected, key=lambda item: item["hour"])
        explanation_lines.extend(
            [
                f"- {item['range']} durante {item['duration_minutes']} min: {item['eur_kwh']:.5f} EUR/kWh, coste aprox {item['estimated_cost_eur']:.4f} EUR"
                for item in selected_chronological
            ]
        )
        explanation_lines.append(f"Total estimado: {total_kwh:.3f} kWh, {total_cost:.4f} EUR.")
    else:
        explanation_lines.append("No quedan horas elegibles en la ventana indicada con precios PVPC disponibles.")
    explanation_lines.append("No voy a agendarlo hasta que confirmes. ¿Quieres corregir potencia, presupuesto u horas antes de crear las tareas?")

    return compact_json(
        {
            "recommended_answer": "\n".join(explanation_lines),
            "requires_user_confirmation_before_scheduling": True,
            "budget_eur": round(budget_eur, 2),
            "timezone": timezone_name,
            "now_local": now_local.isoformat(timespec="seconds"),
            "window": {"start_hour": start_hour_int, "end_hour": end_hour_int, "only_future": only_future},
            "assumptions": {
                "climate_entity_id": climate_entity_id,
                "ac_power_kw": ac_power_kw,
                "power_sensor_entity_id": power_sensor_entity_id,
                "power_delta_threshold_w": AC_POWER_DELTA_THRESHOLD_W,
                "current_power_w": current_power_w,
            },
            "selected_slots": selected,
            "estimated_total_kwh": round(total_kwh, 3),
            "estimated_total_cost_eur": round(total_cost, 4),
            "scheduling_policy": (
                "Si el usuario confirma, agenda pares input_boolean.turn_on/input_boolean.turn_off para input_boolean.brokton_ac_dp1_switch en las franjas seleccionadas. "
                "Cada accion debe verificarse con power_verification de ha_call_service."
            ),
        },
        max_chars=30000,
    )


async def ha_count_state_transitions(context: Any, args: dict[str, Any]) -> str:
    start_time = normalize_history_iso(str(args.get("start_time", "") or "").strip())
    if not start_time:
        raise ValueError("start_time es obligatorio")
    end_time = normalize_history_iso(str(args.get("end_time", "") or "").strip())
    from_state = str(args.get("from_state", "off") or "off").strip().lower()
    to_state = str(args.get("to_state", "on") or "on").strip().lower()
    entity_ids_arg = args.get("entity_ids")
    entity_id = str(args.get("entity_id", "") or "").strip()
    entity_ids: list[str] = []
    if isinstance(entity_ids_arg, list):
        entity_ids = [str(item).strip() for item in entity_ids_arg if str(item).strip()]
    elif entity_id:
        entity_ids = [entity_id]
    else:
        entity_ids = await resolve_history_entities(context, args)
    if not entity_ids:
        return compact_json({"entities": [], "message": "No se encontraron entidades para contar transiciones."})

    httpx = httpx_module(context)
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as http:
        for candidate in entity_ids:
            resolved, _warnings = await resolve_entity_reference(
                context,
                entity_id=candidate,
                domain=str(args.get("domain", "") or "").strip().lower(),
            )
            candidate = resolved or candidate
            params: dict[str, str] = {
                "minimal_response": "1",
                "no_attributes": "1",
                "filter_entity_id": candidate,
            }
            if end_time:
                params["end_time"] = end_time
            response = await http.get(
                f"{ha_base_url(context)}/api/history/period/{start_time}",
                headers=ha_headers(context),
                params=params,
            )
            response.raise_for_status()
            points = useful_history_points(flatten_history_points(response.json()))
            transitions: list[dict[str, Any]] = []
            previous_state: str | None = None
            for point in points:
                state = str(point.get("state", "") or "").lower()
                if state == to_state and previous_state == from_state:
                    transitions.append(point)
                previous_state = state
            rows.append(
                {
                    "entity_id": candidate,
                    "from_state": from_state,
                    "to_state": to_state,
                    "count": len(transitions),
                    "transition_times": [point.get("last_changed") for point in transitions],
                    "points": len(points),
                    "first": points[0] if points else None,
                    "last": points[-1] if points else None,
                }
            )
    return compact_json(
        {
            "start_time": start_time,
            "end_time": end_time or None,
            "from_state": from_state,
            "to_state": to_state,
            "entities_checked": len(entity_ids),
            "total_transitions": sum(row["count"] for row in rows),
            "entities_with_transitions": sum(1 for row in rows if row["count"] > 0),
            "results": rows,
        },
        max_chars=30000,
    )


async def ha_get_logbook(context: Any, args: dict[str, Any]) -> str:
    start_time = str(args.get("start_time", "") or "").strip()
    if not start_time:
        raise ValueError("start_time es obligatorio")
    params: dict[str, str] = {}
    end_time = str(args.get("end_time", "") or "").strip()
    entity_id = str(args.get("entity_id", "") or "").strip()
    if end_time:
        params["end_time"] = end_time
    if entity_id:
        resolved, _warnings = await resolve_entity_reference(context, entity_id=entity_id, domain=str(args.get("domain", "") or ""))
        params["entity"] = resolved or entity_id
    httpx = httpx_module(context)
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"{ha_base_url(context)}/api/logbook/{start_time}", headers=ha_headers(context), params=params)
        response.raise_for_status()
        return compact_json(response.json(), max_chars=12000)
