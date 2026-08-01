from __future__ import annotations


NUMERIC_HISTORY_CLASS_TERMS: dict[str, set[str]] = {
    "water": {"agua", "litro", "litros"},
    "energy": {"energia", "electricidad", "kwh", "suministro", "suministrado", "suministrada"},
    "power": {"potencia", "watio", "watios"},
    "temperature": {"temperatura", "calor", "frio"},
    "humidity": {"humedad"},
}

GENERIC_HISTORY_TOKENS: set[str] = {
    "que", "cual", "dia", "semana", "pasada", "anterior", "consumi", "consumo",
    "energia", "electricidad", "agua", "litros", "temperatura", "humedad", "potencia",
    "mas", "menos", "mayor", "menor", "maximo", "maxima", "minimo", "minima",
    "lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo",
    "del", "mes", "ultimo", "ultima", "este", "ano", "actual",
    "total", "suma", "suministro", "suministrado", "suministrada",
    "hoy", "ayer", "anteayer",
}


def requested_numeric_device_classes(query_tokens: set[str]) -> set[str]:
    return {
        device_class
        for device_class, terms in NUMERIC_HISTORY_CLASS_TERMS.items()
        if query_tokens.intersection(terms)
    }


def numeric_history_scope_tokens(query_tokens: set[str], month_names: tuple[str, ...]) -> set[str]:
    return query_tokens - (GENERIC_HISTORY_TOKENS | set(month_names))


def is_consumption_query(folded_text: str) -> bool:
    return any(term in folded_text for term in ("consum", "gasto", "gastado", "usado"))


def is_numeric_consumption_query(folded_text: str) -> bool:
    return any(
        term in folded_text
        for term in ("consum", "gasto", "gastado", "usado", "energia", "electricidad", "agua", "litro", "suministr")
    )


def is_explicit_water_query(folded_text: str) -> bool:
    return any(term in folded_text for term in ("agua", "litro"))


def is_energy_query(folded_text: str, *, explicit_water_query: bool) -> bool:
    return any(term in folded_text for term in ("energia", "electricidad", "kwh", "suministr")) or (
        "consumo total" in folded_text and not explicit_water_query
    )
