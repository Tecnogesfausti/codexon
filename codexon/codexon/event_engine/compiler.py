from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

from automation.schema import validate_plan


EntityResolver = Callable[[str], tuple[str, str] | None]


@dataclass(frozen=True)
class CompiledEventListener:
    title: str
    summary: str
    entity_id: str
    from_state: str
    to_state: str
    plan: dict


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).casefold()


def compile_state_action_listener(
    user_text: str, *, resolve_entity: EntityResolver
) -> CompiledEventListener | None:
    """Compile simple `cuando X cambie, haz Y` requests without an LLM."""
    folded = _fold(user_text.strip())
    match = re.fullmatch(
        r"\s*(?:cuando|en cuanto)\s+(?:se\s+)?"
        r"(?P<trigger>encienda|enciendas|encender|active|actives|activar|"
        r"apague|apagues|apagar|desactive|desactives|desactivar)\s+"
        r"(?P<source>.+?)\s*,?\s+"
        r"(?P<action>enciende|encender|activa|activar|apaga|apagar|"
        r"desactiva|desactivar)\s+(?P<target>.+?)\s*[.!]?\s*",
        folded,
    )
    if match is None:
        return None
    source_text = match.group("source").strip()
    target_text = match.group("target").strip()
    if "luz" in source_text and re.match(r"^la\s+(?:del|de\s+la)\b", target_text):
        target_text = re.sub(r"^la\s+", "luz ", target_text, count=1)
    source = resolve_entity(source_text)
    target = resolve_entity(target_text)
    if source is None or target is None:
        return None
    source_entity, source_label = source
    target_entity, target_label = target
    trigger_on = match.group("trigger").startswith(("enciend", "activ"))
    action_on = match.group("action").startswith(("enciend", "activ"))
    from_state, to_state = ("off", "on") if trigger_on else ("on", "off")
    service, expected_state = ("turn_on", "on") if action_on else ("turn_off", "off")
    plan = validate_plan(
        {
            "version": 1,
            "name": f"{service} {target_label} al cambiar {source_label}",
            "conditions": [],
            "steps": [
                {
                    "type": "service",
                    "domain": target_entity.split(".", 1)[0],
                    "service": service,
                    "target": {"entity_id": target_entity},
                    "confirm": True,
                    "expected_state": expected_state,
                    "verify_entity_id": target_entity,
                }
            ],
        }
    )
    return CompiledEventListener(
        title=f"{source_label}: {to_state} → {service} {target_label}",
        summary=(
            f"cuando {source_label} ({source_entity}) cambie {from_state}→{to_state}, "
            f"ejecutará {target_entity.split('.', 1)[0]}.{service} sobre "
            f"{target_label} ({target_entity}) y verificará estado {expected_state}"
        ),
        entity_id=source_entity,
        from_state=from_state,
        to_state=to_state,
        plan=plan,
    )
