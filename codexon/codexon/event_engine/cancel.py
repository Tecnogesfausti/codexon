from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


_CANCEL_WORDS = {
    "automatizacion",
    "escucha",
    "regla",
    "evento",
    "cancela",
    "cancelar",
    "elimina",
    "eliminar",
    "desactiva",
    "desactivar",
    "para",
    "deten",
    "detener",
    "que",
    "la",
    "el",
    "al",
    "del",
    "de",
    "una",
    "un",
}


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).casefold()


def _token(value: str) -> str:
    if value.startswith("encend"):
        return "encender"
    if value.startswith("apag"):
        return "apagar"
    if value.startswith("desactiv"):
        return "desactivar"
    if value.startswith("activ"):
        return "activar"
    return value


def _tokens(value: str) -> set[str]:
    return {
        _token(part)
        for part in re.findall(r"[a-z0-9_]+", _fold(value))
        if len(part) > 1 and part not in _CANCEL_WORDS
    }


def is_listener_cancel_request(user_text: str) -> bool:
    folded = _fold(user_text)
    if re.search(r"\bno\s+(?:canceles|desactives|elimines|pares)\b", folded):
        return False
    has_cancel_verb = bool(
        re.search(
            r"\b(?:cancela|cancelar|elimina|eliminar|desactiva|desactivar|"
            r"para|deten|detener)\b",
            folded,
        )
    )
    has_listener_noun = bool(
        re.search(r"\b(?:automatizacion|escucha|regla|suscripcion)\b", folded)
    )
    return has_cancel_verb and has_listener_noun


def select_listener_candidate(
    user_text: str, candidates: Iterable[tuple[int, str]]
) -> int | None:
    rows = list(candidates)
    if not rows:
        return None
    folded = _fold(user_text)
    explicit = re.search(r"\b(?:escucha|automatizacion|regla)\s*#?\s*(\d+)\b", folded)
    if explicit:
        requested_id = int(explicit.group(1))
        return requested_id if any(item[0] == requested_id for item in rows) else None
    request_tokens = _tokens(user_text)
    scored = sorted(
        (
            (len(request_tokens & _tokens(search_text)), listener_id)
            for listener_id, search_text in rows
        ),
        reverse=True,
    )
    if scored and scored[0][0] >= 2:
        if len(scored) == 1 or scored[0][0] > scored[1][0]:
            return scored[0][1]
    if len(rows) == 1 and re.search(
        r"\b(?:esa|esta|la)\s+(?:automatizacion|escucha|regla)\b", folded
    ):
        return rows[0][0]
    return None
