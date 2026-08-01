from __future__ import annotations

import re

from .text import normalize_text


def requested_physical_light_action(user_text: str) -> bool:
    folded = normalize_text(user_text)
    if not re.search(r"\bluz(?:es)?\b", folded):
        return False
    return any(
        re.search(pattern, folded)
        for pattern in (
            r"\benciend[ea]?\b",
            r"\bencender\b",
            r"\bapag[ao]?\b",
            r"\bapagar\b",
            r"\bprend[ea]?\b",
            r"\bponer\b",
            r"\bpon\b",
            r"\bquitar\b",
            r"\bactiva[rd]?\b",
            r"\bdesactiva[rd]?\b",
        )
    )


def requested_power_supply_status(user_text: str) -> bool:
    folded = normalize_text(user_text)
    if requested_physical_light_action(folded):
        return False
    return any(
        term in folded
        for term in (
            "se ha ido la luz",
            "se fue la luz",
            "se fue el suministro",
            "corte de luz",
            "corte electrico",
            "corte de corriente",
            "sin luz",
            "sin corriente",
            "hay luz",
            "ha vuelto la luz",
            "volvio la luz",
            "hay corriente",
            "suministro electrico",
        )
    )


def requested_light_sensation(user_text: str) -> bool:
    folded = normalize_text(user_text)
    if requested_physical_light_action(folded) or requested_power_supply_status(folded):
        return False
    return any(
        term in folded
        for term in (
            "cuanta luz",
            "nivel de luz",
            "medir luz",
            "luz interior",
            "luz exterior",
            "luminosidad",
            "sensacion luminica",
            "iluminancia",
            "lux",
            "radiacion solar",
        )
    )
