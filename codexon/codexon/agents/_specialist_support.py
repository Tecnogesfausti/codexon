from __future__ import annotations

import datetime as dt
import fnmatch
import unicodedata
from typing import Any, Iterable

from agents.monitor_temperatura import HomeAssistantRestReader, parse_datetime


def resolve_ha_client(context: Any) -> Any:
    injected = context.services.get("ha_client")
    if injected is not None:
        return injected
    codexon = context.services.get("codexon")
    if codexon is None or not getattr(codexon, "has_homeassistant_rest", False):
        raise RuntimeError("missing_homeassistant_rest")
    return HomeAssistantRestReader(codexon)


def folded(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).casefold()


def state_text(state: dict[str, Any]) -> str:
    attrs = state.get("attributes") or {}
    return folded(
        " ".join(
            str(item or "")
            for item in (
                state.get("entity_id"),
                attrs.get("friendly_name"),
                attrs.get("device_class"),
                attrs.get("unit_of_measurement"),
            )
        )
    )


def is_on(state: dict[str, Any]) -> bool:
    return folded(state.get("state")) in {"on", "open", "opening", "home", "detected", "active", "activo"}


def number(state: dict[str, Any]) -> float | None:
    try:
        return float(str(state.get("state")).replace(",", ".").split()[0])
    except (IndexError, TypeError, ValueError):
        return None


def entity_id(state: dict[str, Any]) -> str:
    return str(state.get("entity_id") or "")


def friendly_name(state: dict[str, Any]) -> str:
    return str((state.get("attributes") or {}).get("friendly_name") or entity_id(state))


def matches_any(state: dict[str, Any], terms: Iterable[str]) -> bool:
    text = state_text(state)
    return any(folded(term) in text for term in terms)


def matches_patterns(state: dict[str, Any], patterns: Iterable[str]) -> bool:
    eid = entity_id(state)
    return any(fnmatch.fnmatch(eid, pattern) for pattern in patterns)


def age_minutes(state: dict[str, Any], now: dt.datetime) -> float | None:
    changed = parse_datetime(state.get("last_updated") or state.get("last_changed"))
    if changed is None:
        return None
    return max(0.0, (now - changed).total_seconds() / 60.0)


def state_evidence(state: dict[str, Any]) -> str:
    attrs = state.get("attributes") or {}
    unit = str(attrs.get("unit_of_measurement") or "")
    return f"{entity_id(state)}={state.get('state')}{(' ' + unit) if unit else ''}"
