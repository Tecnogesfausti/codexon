from __future__ import annotations

from .text import normalize_text


def requested_security_activity(user_text: str) -> dict[str, str] | None:
    folded = normalize_text(user_text)
    if not any(term in folded for term in ("actividad", "movimiento", "presencia", "sensores han saltado", "ha saltado", "han saltado")):
        return None
    if not any(term in folded for term in ("terreno", "huerta", "huerto", "casa", "oficina", "almacen", "garrofera", "olivera", "cancela", "seguridad", "ir", "cam")):
        return None
    area_terms = []
    for term in ("terreno", "huerta", "huerto", "casa", "oficina", "almacen", "garrofera", "olivera", "cancela"):
        if term in folded:
            area_terms.append(term)
    if not area_terms and "seguridad" in folded:
        area_terms.append("")
    if any(term in folded for term in ("5 minutos", "5 min", "ahora", "reciente", "ultimos minutos")):
        window = "5min"
    elif any(term in folded for term in ("hora", "1h", "60 minutos")):
        window = "1h"
    else:
        window = "24h"
    return {"area": " ".join(area_terms).strip(), "window": window}
