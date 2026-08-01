from __future__ import annotations

import unicodedata


KNOWN_ENTITY_ALIASES = (
    ("luz del comedor", "switch.relestftcomedor_rele1", "luz del comedor"),
    ("luz de comedor", "switch.relestftcomedor_rele1", "luz del comedor"),
    ("luz comedor", "switch.relestftcomedor_rele1", "luz del comedor"),
    ("luz de la cocina", "switch.nspanel_relay_2", "luz de la cocina"),
    ("luz de cocina", "switch.nspanel_relay_2", "luz de la cocina"),
    ("luz cocina", "switch.nspanel_relay_2", "luz de la cocina"),
    ("luz del sofa", "switch.nspanel_relay_1", "luz del sofa"),
    ("luz de sofa", "switch.nspanel_relay_1", "luz del sofa"),
    ("luz sofa", "switch.nspanel_relay_1", "luz del sofa"),
)


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def resolve_known_entity_alias(text: str) -> tuple[str, str] | None:
    folded = _fold(text)
    return next(
        ((entity_id, label) for alias, entity_id, label in KNOWN_ENTITY_ALIASES if alias in folded),
        None,
    )
