from __future__ import annotations

import re
import unicodedata


def fold_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    return "".join(char for char in text if not unicodedata.combining(char))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", fold_accents(value).lower()).strip()
