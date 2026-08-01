#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOMEASSISTANT = ROOT / "tools" / "homeassistant.py"
REGISTRY = ROOT / "tools" / "registry.py"
CODEXON = ROOT / "codexon.py"


HELPER_CODE = r'''

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
    if entity_id and entity_id == entity_id.lower():
        return entity_id, warnings
    if entity_id:
        search_args = dict(args)
        search_args["query"] = str(args.get("query") or entity_search_text(entity_id))
        search_args["domain"] = str(args.get("domain") or entity_id.split(".", 1)[0] if "." in entity_id else "sensor")
        entity_ids = await resolve_history_entities(context, search_args)
        if entity_ids:
            resolved = entity_ids[0]
            if resolved != entity_id:
                warnings.append(f"entity_resolved:{entity_id}->{resolved}")
            return resolved, warnings
        lowered = entity_id.lower()
        warnings.append(f"entity_lowercased:{entity_id}->{lowered}")
        return lowered, warnings
    entity_ids = await resolve_history_entities(context, args)
    if entity_ids:
        return entity_ids[0], warnings
    return "", warnings
'''


TOOL_CODE = r'''

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
'''


REGISTRY_ENTRY = '''    "ha_get_history_around_time": BuiltinTool(
        name="ha_get_history_around_time",
        description=(
            "Consulta el valor historico de una entidad alrededor de una hora local durante varios dias. "
            "Usala para preguntas como 'temperatura alrededor de las 10am de los ultimos 7 dias'."
        ),
        parameters=object_schema(
            {
                "entity_id": {"type": "string", "description": "Entidad concreta de Home Assistant."},
                "local_time": {"type": "string", "description": "Hora local objetivo, por ejemplo 10:00 o 10am.", "default": "10:00"},
                "days": {"type": "integer", "default": 7},
                "window_minutes": {"type": "integer", "default": 30},
                "timezone": {"type": "string", "default": "Europe/Madrid"},
                "end_date": {"type": "string", "description": "Fecha final local YYYY-MM-DD. Opcional."},
                "query": {"type": "string", "description": "Texto para buscar entidad si no das entity_id."},
                "domain": {"type": "string", "default": "sensor"},
                "device_class": {"type": "string", "description": "device_class opcional, por ejemplo temperature."},
            },
            ["local_time"],
        ),
        handler=homeassistant.ha_get_history_around_time,
    ),
'''


def backup(path: Path) -> None:
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{stamp}")
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def replace_async_function(text: str, function_name: str, replacement: str) -> tuple[str, bool]:
    marker = f"\nasync def {function_name}"
    start = text.find(marker)
    if start == -1:
        return text, False
    next_start = text.find("\n\nasync def ", start + len(marker))
    if next_start == -1:
        return text, False
    return text[:start] + replacement + text[next_start:], True


def patch_homeassistant() -> bool:
    text = HOMEASSISTANT.read_text(encoding="utf-8")
    changed = False
    if "from zoneinfo import ZoneInfo" not in text:
        text = text.replace("import re\nfrom typing import Any\n", "import datetime as dt\nimport re\nfrom typing import Any\nfrom zoneinfo import ZoneInfo\n", 1)
        changed = True
    if "def parse_ha_datetime" not in text:
        text = text.replace("\n\nasync def ha_get_states", HELPER_CODE + "\n\nasync def ha_get_states", 1)
        changed = True
    elif "resolve_entity_for_history_around_time" not in text:
        text = text.replace("\n\nasync def ha_get_states", HELPER_CODE + "\n\nasync def ha_get_states", 1)
        changed = True
    if "async def ha_get_history_around_time" in text:
        text, replaced = replace_async_function(text, "ha_get_history_around_time", TOOL_CODE)
        changed = changed or replaced
    else:
        text = text.replace("\n\nasync def resolve_history_entities", TOOL_CODE + "\n\nasync def resolve_history_entities", 1)
        changed = True
    if changed:
        backup(HOMEASSISTANT)
        HOMEASSISTANT.write_text(text, encoding="utf-8")
    return changed


def patch_registry() -> bool:
    text = REGISTRY.read_text(encoding="utf-8")
    if "ha_get_history_around_time" in text:
        return False
    text = text.replace('    "ha_get_logbook": BuiltinTool(\n', REGISTRY_ENTRY + '    "ha_get_logbook": BuiltinTool(\n', 1)
    backup(REGISTRY)
    REGISTRY.write_text(text, encoding="utf-8")
    return True


def patch_prompt() -> bool:
    if not CODEXON.exists():
        return False
    text = CODEXON.read_text(encoding="utf-8")
    old = "- Para preguntas históricas usa ha_get_history o ha_get_logbook si están disponibles.\n"
    new = "- Para preguntas históricas usa ha_get_history o ha_get_logbook si están disponibles. Para valores alrededor de una hora local durante varios días usa ha_get_history_around_time.\n"
    if old not in text or new in text:
        return False
    backup(CODEXON)
    CODEXON.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


def main() -> int:
    missing = [path for path in (HOMEASSISTANT, REGISTRY) if not path.exists()]
    if missing:
        print("Ejecuta este instalador desde /data/codexon/app.")
        for path in missing:
            print(f"No existe: {path}")
        return 2

    changes = {
        "tools/homeassistant.py": patch_homeassistant(),
        "tools/registry.py": patch_registry(),
        "codexon.py": patch_prompt(),
    }
    for name, changed in changes.items():
        print(f"{name}: {'actualizado' if changed else 'ya estaba'}")
    print("\nReinicia el add-on o el proceso Codexon para cargar la herramienta nueva.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
