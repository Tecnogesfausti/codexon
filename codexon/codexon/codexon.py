#!/usr/bin/env python3
"""
Codexon: agente de terminal 24/7 con MCP, OpenRouter/DeepSeek y memoria local.

Uso:
  python3 codexon.py
  python3 codexon.py --mcp-url http://supervisor/core/api/mcp
  python3 codexon.py -- npx -y <servidor-mcp-ha>

Comandos dentro del terminal:
  /help                 Muestra ayuda
  /memoria              Lista memorias guardadas
  /sensores             Lista observaciones recientes
  /aprender <texto>     Guarda una memoria manual
  /consultalo <texto>   Prepara una nota para que Codex revise/corrija Codexon
  /codex <texto>        Abre Codex para enseñar o modificar Codexon
  /estado               Muestra modelo, MCP y tareas activas
  /salir                Cierra el agente
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import dataclasses
import datetime as dt
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from automation import (
    AutomationExecutor,
    automation_plan_json_schema,
    compile_binary_transition,
    compile_numeric_condition,
    compile_timed_alternation,
    decode_legacy_plan,
    decode_plan,
    encode_plan,
    validate_plan,
)
from agents.manager import AgentManager
from event_engine import (
    EventEngine,
    compile_state_action_listener,
    ensure_event_schema,
    is_listener_cancel_request,
    select_listener_candidate,
)
from event_engine.storage import (
    create_subscription,
    list_subscriptions,
    set_subscription_enabled,
)
from intent.consumption import (
    NUMERIC_HISTORY_CLASS_TERMS,
    is_consumption_query,
    is_energy_query,
    is_explicit_water_query,
    is_numeric_consumption_query,
    numeric_history_scope_tokens,
    requested_numeric_device_classes,
)
from intent.environment import requested_climate_strategy, requested_environment_sensor
from intent.lights import requested_light_sensation, requested_physical_light_action, requested_power_supply_status
from intent.security import requested_security_activity
from intent.water import WATER_TERMS, infer_water_entity_roles, water_role_hints
from planning.climate import (
    environment_state_value,
    format_climate_strategy_answer,
    format_environment_sensor_answer,
)
from planning.light import format_light_sensation_answer
from planning.power import format_power_supply_status_answer
from scheduler.metrics import format_scheduler_monitor, scheduler_monitor
from scheduler.runner import run_task_loop
from services.whatsapp_bridge import WhatsAppBridge, WhatsAppBridgeConfig
from site_profile import SiteProfile, SiteProfileError
from tools.registry import builtin_tool_names, builtin_tool_schemas, call_builtin_tool as call_indexed_builtin_tool
from tools.traccar import requested_tracker
from services.live_context.manager import LiveContextManager

try:
    import readline
except ModuleNotFoundError:
    readline = None  # type: ignore[assignment]

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    try:
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        from mcp.client.streamable_http import (
            create_mcp_http_client as create_mcp2_http_client,
            streamable_http_client as streamable_http2_client,
        )

        @contextlib.asynccontextmanager
        async def streamablehttp_client(
            url: str,
            headers: dict[str, str] | None = None,
        ):
            async with create_mcp2_http_client(headers=headers) as http_client:
                async with streamable_http2_client(
                    url,
                    http_client=http_client,
                ) as (read, write):
                    yield read, write, lambda: None
    import httpx
    from openai import AsyncOpenAI
    from dotenv import load_dotenv
    import yaml
except ModuleNotFoundError as exc:
    MISSING_DEPENDENCY = exc.name
    ClientSession = Any  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    streamablehttp_client = None  # type: ignore[assignment]
    httpx = None  # type: ignore[assignment]
    AsyncOpenAI = None  # type: ignore[assignment]
    load_dotenv = None  # type: ignore[assignment]
    yaml = None  # type: ignore[assignment]
else:
    MISSING_DEPENDENCY = None


APP_NAME = "Codexon"
DEFAULT_MODEL = "openrouter/free"
FALLBACK_MODEL = "openrouter/free"
DEFAULT_DB = "codexon_memory.sqlite3"
DEFAULT_POLL_SECONDS = 300
DEFAULT_MODEL_ROUTES = os.getenv("CODEXON_MODEL_ROUTES", "model_routes.yaml")
DEFAULT_AGENTS_DIR = os.getenv("CODEXON_AGENTS_DIR", "agents")
DEFAULT_SITE_PROFILE = os.getenv(
    "CODEXON_SITE_PROFILE",
    str(Path(os.getenv("CODEXON_DB", "data/codexon_memory.sqlite3")).expanduser().parent / "site.yaml"),
)
MAX_TOOL_ROUNDS = 12
MAX_HISTORY_MESSAGES = 24
DEFAULT_TIMEZONE = os.getenv("CODEXON_TIMEZONE", "Europe/Madrid")
TASK_TIMEOUT_SECONDS = int(os.getenv("CODEXON_TASK_TIMEOUT_SECONDS", "120"))
TASK_POLL_MAX_SECONDS = max(0.1, float(os.getenv("CODEXON_TASK_POLL_MAX_SECONDS", "1")))
DEFAULT_TASK_MAX_ATTEMPTS = max(1, int(os.getenv("CODEXON_TASK_MAX_ATTEMPTS", "3")))
DEFAULT_TASK_RETRY_BACKOFF_SECONDS = max(1, int(os.getenv("CODEXON_TASK_RETRY_BACKOFF_SECONDS", "30")))
MAX_TASK_INTERVAL_SECONDS = 315_360_000
TASK_LEASE_GRACE_SECONDS = 30
DEFAULT_FS_ROOTS = os.getenv("CODEXON_FS_ROOTS", str(Path.cwd()))
DEFAULT_LOG_FILE = os.getenv("CODEXON_LOG_FILE", "codexon_runtime.log")
DEFAULT_HISTORY_FILE = os.getenv("CODEXON_HISTORY_FILE", ".codexon_history")
DEFAULT_HISTORY_LIMIT = int(os.getenv("CODEXON_HISTORY_LIMIT", "1000"))
DEFAULT_LAST_INTERACTION_FILE = os.getenv("CODEXON_LAST_INTERACTION", "/data/codexon/last-console-interaction.json")
PHRASE_TRIGGERS_SETTING = "terminal.phrase_triggers"



def configure_runtime_defaults_from_env() -> None:
    global DEFAULT_TIMEZONE, TASK_TIMEOUT_SECONDS, DEFAULT_FS_ROOTS, DEFAULT_LOG_FILE, DEFAULT_HISTORY_FILE, DEFAULT_HISTORY_LIMIT

    DEFAULT_TIMEZONE = os.getenv("CODEXON_TIMEZONE", DEFAULT_TIMEZONE)
    DEFAULT_FS_ROOTS = os.getenv("CODEXON_FS_ROOTS", DEFAULT_FS_ROOTS)
    DEFAULT_LOG_FILE = os.getenv("CODEXON_LOG_FILE", DEFAULT_LOG_FILE)
    DEFAULT_HISTORY_FILE = os.getenv("CODEXON_HISTORY_FILE", DEFAULT_HISTORY_FILE)
    history_limit_text = os.getenv("CODEXON_HISTORY_LIMIT")
    if history_limit_text:
        with contextlib.suppress(ValueError):
            DEFAULT_HISTORY_LIMIT = max(1, int(history_limit_text))
    timeout_text = os.getenv("CODEXON_TASK_TIMEOUT_SECONDS")
    if timeout_text:
        try:
            TASK_TIMEOUT_SECONDS = max(1, int(timeout_text))
        except ValueError:
            runtime_log(
                "warn",
                "config",
                "CODEXON_TASK_TIMEOUT_SECONDS invalido; se mantiene valor anterior",
                value=timeout_text,
            )


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def next_interval_run_at(previous_run_at: str, interval_seconds: int) -> str:
    interval = max(1, int(interval_seconds))
    try:
        next_run = dt.datetime.fromisoformat(parse_datetime_to_utc(previous_run_at))
    except Exception:
        next_run = dt.datetime.now(dt.UTC)
    now = dt.datetime.now(dt.UTC)
    if next_run <= now:
        elapsed = (now - next_run).total_seconds()
        skipped = int(elapsed // interval) + 1
        next_run += dt.timedelta(seconds=skipped * interval)
    return next_run.isoformat(timespec="seconds")


def validate_task_interval(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    interval = int(value)
    if not 1 <= interval <= MAX_TASK_INTERVAL_SECONDS:
        raise ValueError(f"interval_seconds debe estar entre 1 y {MAX_TASK_INTERVAL_SECONDS}")
    return interval


def task_recovery_policy(instruction: str) -> str:
    lowered = fold_accents(instruction.lower())
    non_idempotent_markers = (
        "automation_plan_v1",
        "deterministic_water_liters",
        "button.",
        "ha_press_entity_interval",
        "service=press",
        '"service":"press"',
        "toggle",
        "pulsa",
        "pulsar",
        "sirena",
        "fireworks",
        "notify/",
        "ha_send_mobile_alert",
        "google_translate_say",
        "rtttl",
        "tono",
        "por voz",
        "repetir",
        "veces",
        "ha_call_service",
        "turn_on",
        "turn_off",
        "enciende",
        "encender",
        "apaga",
        "apagar",
        "activa",
        "activar",
        "desactiva",
        "desactivar",
        "abre",
        "abrir",
        "cierra",
        "cerrar",
    )
    return "manual" if any(marker in lowered for marker in non_idempotent_markers) else "retry"


def format_task_time(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        local = parsed.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
        return f"{local.strftime('%Y-%m-%d %H:%M:%S')} ({DEFAULT_TIMEZONE})"
    except Exception:
        return str(value)


def format_interval(seconds: int | None) -> str:
    if not seconds:
        return "una sola vez"
    seconds = int(seconds)
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"cada {days} dia" if days == 1 else f"cada {days} dias"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"cada {hours} hora" if hours == 1 else f"cada {hours} horas"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"cada {minutes} minuto" if minutes == 1 else f"cada {minutes} minutos"
    return f"cada {seconds} segundos"


def summarize_task_text(value: str | None, *, max_len: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_len:
        return text or "-"
    return text[: max_len - 1].rstrip() + "…"


def format_task_row(row: sqlite3.Row) -> str:
    interval = format_interval(row["interval_seconds"] if "interval_seconds" in row.keys() else None)
    cancel_key = row["cancellation_key"] if "cancellation_key" in row.keys() else None
    detail = row["last_error"] or row["result"] or row["instruction"]
    parts = [
        f"#{row['id']} [{row['status']}] {row['title']}",
        f"  Próxima: {format_task_time(row['run_at'])}",
        f"  Frecuencia: {interval}",
    ]
    if "attempts" in row.keys() and "max_attempts" in row.keys() and int(row["attempts"] or 0):
        parts.append(f"  Intentos: {int(row['attempts'])}/{int(row['max_attempts'])}")
    if cancel_key:
        parts.append(f"  Clave de cancelación: {cancel_key}")
    parts.append(f"  Detalle: {summarize_task_text(detail)}")
    return "\n".join(parts)


def format_task_list(rows: list[sqlite3.Row], *, include_done: bool = False) -> str:
    if not rows:
        return "No hay tareas programadas activas." if not include_done else "No hay tareas registradas."
    header = "Tareas programadas" if include_done else "Tareas activas"
    return header + ":\n" + "\n".join(format_task_row(row) for row in rows)


def runtime_log(level: str, component: str, message: str, **fields: Any) -> None:
    payload = {
        "ts": utc_now(),
        "level": level,
        "component": component,
        "message": message,
        **fields,
    }
    path = Path(DEFAULT_LOG_FILE).expanduser()
    with contextlib.suppress(Exception):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def tail_file(path: Path, limit: int = 50) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max(1, min(limit, 500)) :]


def local_now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(DEFAULT_TIMEZONE))


SPANISH_MONTH_NAMES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)

SPANISH_WEEKDAY_INDEX = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "domingo": 6,
}

ENERGY_SUPPLY_GROUP_LABELS = {
    "casa": "Casa",
    "oficina": "Oficina",
    "tv": "TV",
    "fuera": "Exterior",
    "almacenherramientas": "Almacen herramientas",
}

ENERGY_SUPPLY_SCOPE_TERMS = {
    "casa": {"casa", "interior", "enchufes"},
    "oficina": {"oficina", "office", "despacho"},
    "tv": {"tv", "tele", "television"},
    "fuera": {"fuera", "exterior", "luces", "persianas"},
    "almacenherramientas": {"almacen", "caseta", "herramientas"},
}


def previous_calendar_week_range(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    current_week_start = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return current_week_start - dt.timedelta(days=7), current_week_start


def requested_weekday_date(user_text: str, now: dt.datetime) -> dt.date | None:
    folded = fold_accents(user_text.lower())
    weekday = next((index for name, index in SPANISH_WEEKDAY_INDEX.items() if re.search(rf"\b{name}\b", folded)), None)
    if weekday is None:
        return None
    if any(term in folded for term in ("semana pasada", "semana anterior")):
        current_week_start = now.date() - dt.timedelta(days=now.weekday())
        return current_week_start - dt.timedelta(days=7) + dt.timedelta(days=weekday)
    return now.date() - dt.timedelta(days=(now.weekday() - weekday) % 7)


def requested_history_date(user_text: str, now: dt.datetime) -> dt.date | None:
    folded = fold_accents(user_text.lower())
    if re.search(r"\banteayer\b", folded):
        return now.date() - dt.timedelta(days=2)
    if re.search(r"\bayer\b", folded):
        return now.date() - dt.timedelta(days=1)
    if re.search(r"\bhoy\b", folded):
        return now.date()
    month_pattern = "|".join(SPANISH_MONTH_NAMES)
    explicit_date_match = re.search(
        rf"\b(?:el\s+)?(?:dia\s+)?(?P<day>[0-3]?\d)\s+de\s+(?P<month>{month_pattern})"
        r"(?:\s+de\s+(?P<year>\d{4}|este\s+ano|ano\s+actual))?\b",
        folded,
    )
    if explicit_date_match:
        month = SPANISH_MONTH_NAMES.index(explicit_date_match.group("month")) + 1
        year_text = explicit_date_match.group("year") or ""
        if year_text.isdigit():
            year = int(year_text)
        else:
            year = now.year
            if not year_text and month > now.month:
                year -= 1
        with contextlib.suppress(ValueError):
            return dt.date(year, month, int(explicit_date_match.group("day")))
        return None
    previous_month_match = re.search(
        r"\b(?:dia\s+)?(?P<day>[0-3]?\d)\s+(?:del\s+)?mes\s+(?:anterior|pasado)\b",
        folded,
    )
    if previous_month_match:
        previous_month = now.date().replace(day=1) - dt.timedelta(days=1)
        with contextlib.suppress(ValueError):
            return previous_month.replace(day=int(previous_month_match.group("day")))
        return None
    return requested_weekday_date(user_text, now)


def requested_energy_supply_scope(user_text: str) -> str | None:
    tokens = set(tokenize_search_query(user_text))
    return next(
        (group for group, terms in ENERGY_SUPPLY_SCOPE_TERMS.items() if tokens.intersection(terms)),
        None,
    )


def energy_supply_group(entity_id: str) -> str | None:
    prefix = "sensor.energiap_"
    normalized = entity_id.lower()
    if not normalized.startswith(prefix):
        return None
    group = normalized.removeprefix(prefix).removesuffix("_diario")
    if group == "office":
        group = "oficina"
    return group if group in ENERGY_SUPPLY_GROUP_LABELS else None


def fold_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    return "".join(char for char in text if not unicodedata.combining(char))


SIGNIFICANT_SHORT_SEARCH_TERMS = frozenset({"ac", "ia", "pc", "tv"})


def tokenize_search_query(value: str) -> list[str]:
    tokens = re.split(r"[^a-z0-9]+", fold_accents(value).lower())
    return [token for token in tokens if len(token) >= 3 or token in SIGNIFICANT_SHORT_SEARCH_TERMS]


def parse_datetime_to_utc(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds")


def normalize_cancellation_key(value: str) -> str:
    return re.sub(r"\s+", " ", fold_accents(value).lower()).strip()


def normalize_entity_alias(value: str) -> str:
    return re.sub(r"\s+", " ", fold_accents(value).casefold()).strip()


@dataclasses.dataclass(frozen=True)
class LexicalCorrection:
    original: str
    corrected: str
    reason: str
    requires_confirmation: bool = False


def lexical_correction_suggestion(user_text: str) -> LexicalCorrection | None:
    corrected = user_text
    reasons: list[str] = []
    folded = normalize_entity_alias(user_text)
    action_sensitive = bool(
        re.search(
            r"\bpone\s+(?:el|la|los|las)?\s*(?:aire|a\.?c\.?|ac|clima|luz|riego|sirena|alarma|agua)\b",
            folded,
        )
    )
    if action_sensitive:
        corrected = re.sub(r"\bpone\b", "pon", corrected, count=1, flags=re.IGNORECASE)
        reasons.append("'pone' parece una errata de imperativo; para ordenar una acción sería 'pon'.")
    if re.search(r"\bkilowatio(?:s)?\b", folded):
        corrected = re.sub(r"\bkilowatio(s)?\b", r"kilovatio\1", corrected, flags=re.IGNORECASE)
        reasons.append("'kilowatio' suele escribirse 'kilovatio'.")
    if re.search(r"\bkw\s*/\s*h\b", folded):
        corrected = re.sub(r"\bkw\s*/\s*h\b", "kWh", corrected, flags=re.IGNORECASE)
        reasons.append("Para precio/energía normalmente la unidad es kWh, no kW/h.")
    if corrected == user_text:
        return None
    return LexicalCorrection(
        original=user_text,
        corrected=corrected,
        reason=" ".join(reasons),
        requires_confirmation=action_sensitive,
    )


def query_role_hints(value: str) -> set[str]:
    folded = normalize_entity_alias(value)
    roles: set[str] = set()
    if any(
        term in folded
        for term in (
            "se ha ido la luz",
            "se fue la luz",
            "corte de luz",
            "corte electrico",
            "sin corriente",
            "suministro electrico",
            "ups",
        )
    ):
        roles.add("power_supply_status")
    if any(term in folded for term in ("cuanta luz", "nivel de luz", "luminosidad", "lux", "iluminancia", "sensacion luminica")):
        roles.add("luminosity_sensor")
    if re.search(r"\b(enciende|encender|apaga|apagar|prende|pon|poner|activa|activar|desactiva|desactivar)\b", folded) and re.search(r"\bluz(?:es)?\b", folded):
        roles.add("physical_light")
    if any(term in folded for term in ("persiana", "persianas", "estor", "estores", "balconera")):
        roles.add("cover_blind")
    if "sirena" in folded:
        roles.add("siren")
    roles.update(water_role_hints(folded))
    if "nevera" in folded:
        roles.add("fridge")
    if any(term in folded for term in ("aire acondicionado", "ac", "aircon", "clima", "brokton", "modo eco", "modo de ahorro", "ahorro", "modo nocturno")):
        roles.add("air_conditioner")
    if any(term in folded for term in ("frio", "frío", "calor", "seco", "ventilador")) and "modo" in folded:
        roles.add("air_conditioner")
    return roles


def readonly_entity_inventory_intent(value: str) -> bool:
    folded = normalize_entity_alias(value)
    subject = bool(
        re.search(
            r"\b(?:dispositivos?|entidades?|grifos?|riegos?|reles?|relays?|"
            r"switch(?:es)?|sensores?|luces?|valvulas?)\b",
            folded,
        )
    )
    if not subject:
        return False
    explicit_question = bool(
        re.search(
            r"\b(?:cuantos?|cuantas?|cuales?|lista|listar|listado|muestra|"
            r"ensena|enumera)\b",
            folded,
        )
    )
    requested_all = bool(
        re.search(r"\b(?:dime|dame)\b", folded)
        and re.search(r"\b(?:todos?|todas?|lista|listado|cuales?)\b", folded)
    )
    capability_question = bool(
        re.search(r"\b(?:puedo|se pueden|es posible)\b", folded)
        and re.search(r"\b(?:encender|activar|abrir|usar)\b", folded)
    )
    return explicit_question or requested_all or capability_question


def explicit_whatsapp_send_intent(value: str) -> bool:
    folded = normalize_entity_alias(value)
    if "whatsapp_send_message" in folded:
        return True
    return bool(
        re.search(r"\b(?:envia|enviar|manda|mandar|escribe|escribir)\b", folded)
        and re.search(r"\b(?:whatsapp|whatsup|wasap)\b", folded)
    )


def historical_active_device_measurement_intent(value: str) -> bool:
    """Identify historical consumption that must be attributed to device activity."""
    folded = normalize_entity_alias(value)
    measurement = bool(
        re.search(
            r"\b(?:litros?|l|m3|metros? cubicos?|kwh|wh|kilovatios?(?: hora)?|"
            r"vatios?(?: hora)?|euros?|consumo|consumido|gastado|usado)\b",
            folded,
        )
    )
    if not measurement:
        return False
    historical_period = bool(
        re.search(
            r"\b(?:ayer|anteayer|semana|mes|ano|dia|dias|hora|horas|"
            r"ultima|ultimo|ultimas|ultimos|pasada|pasado|desde|entre|durante)\b",
            folded,
        )
    )
    historical_consumption = bool(
        re.search(
            r"\b(?:ha|han|habia|habian|haya|hayan)?\s*"
            r"(?:consumid[oa]s?|gastad[oa]s?|usad[oa]s?|registrad[oa]s?|regad[oa]s?)\b|"
            r"\b(?:consumio|consumieron|gasto|gastaron|uso|usaron|registro|registraron|"
            r"rego|regaron)\b",
            folded,
        )
    )
    active_device = bool(
        re.search(
            r"\b(?:grifos?|valvulas?|zonas?|riegos?|bombas?|interruptores?|switch(?:es)?|"
            r"reles?|relays?|maquinas?|equipos?|aparatos?|aires? acondicionados?|"
            r"climatizadores?|calefactores?|lavadoras?|secadoras?|lavavajillas)\b",
            folded,
        )
    )
    return historical_period and historical_consumption and active_device


def answer_contains_attributed_measurement(answer: str, result: dict[str, Any]) -> bool:
    total = result.get("total")
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        return False
    normalized_answer = normalize_entity_alias(answer).replace(",", ".")
    unit = normalize_entity_alias(str(result.get("unit") or ""))
    numeric_values: list[float] = []
    for token in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", normalized_answer):
        with contextlib.suppress(ValueError):
            numeric_values.append(float(token))
    total_present = any(
        math.isclose(value, float(total), rel_tol=0.005, abs_tol=0.05)
        for value in numeric_values
    )
    if not total_present:
        return False
    if not unit:
        return True
    unit_patterns = {
        "l": r"\b(?:l|litro|litros)\b",
        "m3": r"\b(?:m3|metro cubico|metros cubicos)\b",
        "kwh": r"\b(?:kwh|kilovatio hora|kilovatios hora)\b",
        "wh": r"\b(?:wh|vatio hora|vatios hora)\b",
    }
    pattern = unit_patterns.get(unit)
    return bool(re.search(pattern, normalized_answer)) if pattern else unit in normalized_answer


def parse_attributed_measurement_result(content_text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(content_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    total = parsed.get("total")
    if (
        not isinstance(total, (int, float))
        or isinstance(total, bool)
        or not math.isfinite(float(total))
    ):
        return None
    return parsed


def attributed_measurement_period_error(
    user_text: str,
    result: dict[str, Any],
    *,
    now: dt.datetime | None = None,
) -> str | None:
    folded = normalize_entity_alias(user_text)
    if not re.search(r"\b(?:esta semana|semana actual)\b", folded):
        return None
    reference = (now or local_now()).astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    expected_start = (reference - dt.timedelta(days=reference.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    try:
        actual_start = dt.datetime.fromisoformat(str(result.get("start_time") or ""))
        actual_end = dt.datetime.fromisoformat(str(result.get("end_time") or ""))
    except ValueError:
        return "El periodo devuelto no contiene fechas ISO validas."
    if actual_start.tzinfo is None:
        actual_start = actual_start.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
    if actual_end.tzinfo is None:
        actual_end = actual_end.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
    actual_start = actual_start.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    actual_end = actual_end.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    start_ok = abs((actual_start - expected_start).total_seconds()) <= 60
    end_ok = abs((actual_end - reference).total_seconds()) <= 15 * 60
    if start_ok and end_ok:
        return None
    return (
        "'Esta semana' es la semana natural local: usa start_time="
        f"{expected_start.isoformat(timespec='seconds')} y end_time="
        f"{reference.isoformat(timespec='seconds')}; no uses los ultimos siete dias "
        "ni extiendas el final al futuro."
    )


def requested_ac_pvpc_budget_plan(user_text: str) -> dict[str, Any] | None:
    folded = normalize_entity_alias(user_text)
    if not any(term in folded for term in ("aire", "aire acondicionado", "ac", "clima", "brokton")):
        return None
    if not any(term in folded for term in ("euro", "euros", "presupuesto", "gasta", "gastame", "maximo", "maxima")):
        return None
    if not any(term in folded for term in ("luz", "pvpc", "kwh", "electricidad", "consumo")):
        return None
    budget_match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:euro|euros|eur|€)", folded)
    budget = float(budget_match.group(1).replace(",", ".")) if budget_match else 1.0
    return {
        "budget_eur": budget,
        "sensor_entity_id": "sensor.pvpc_dh",
        "climate_entity_id": "input_boolean.brokton_ac_dp1_switch",
        "power_sensor_entity_id": "sensor.powcasa_enchufes_powcasa_enchufes_power",
        "ac_power_kw": 1.2,
        "start_hour": 0,
        "end_hour": 24,
        "only_future": True,
        "timezone": "Europe/Madrid",
    }


def requested_ac_pvpc_shutdown_proposal(user_text: str) -> bool:
    folded = normalize_entity_alias(user_text)
    if not any(term in folded for term in ("propones", "recomienda", "recomiendas", "sugiere", "que hora", "cuando")):
        return False
    if not any(term in folded for term in ("apagar", "apague", "apaga", "quitar", "parar")):
        return False
    if not any(term in folded for term in ("aire", "aire acondicionado", "ac", "clima", "brokton")):
        return False
    return any(term in folded for term in ("precio", "kwh", "kw h", "luz", "pvpc"))


def ac_pvpc_price_terms_present(folded: str) -> bool:
    return any(
        term in folded
        for term in (
            "precio",
            "kwh",
            "kw h",
            "kw/h",
            "kilowatio",
            "kilowatt",
            "kilovatio",
            "kilovatio hora",
            "luz",
            "pvpc",
        )
    )


def ac_terms_present(folded: str) -> bool:
    return any(term in folded for term in ("aire", "aire acondicionado", "a.c", "a.c.", "ac", "clima", "brokton"))


def ac_turn_on_terms_present(folded: str) -> bool:
    return bool(re.search(r"\b(?:enciende|encender|activa|activar|pon|pone|poner|prende|prender)\b", folded))


def requested_ac_pvpc_valley_plan(user_text: str) -> bool:
    folded = normalize_entity_alias(user_text)
    if not ac_terms_present(folded):
        return False
    if not ac_pvpc_price_terms_present(folded):
        return False
    wants_on = ac_turn_on_terms_present(folded)
    wants_hold = any(term in folded for term in ("mantenlo", "mantenerlo", "mantener", "hasta que suba", "cuando suba"))
    wants_low = any(term in folded for term in ("baje de precio", "baje el precio", "baje", "barato", "barata"))
    return wants_on and wants_hold and wants_low


def requested_ac_until_price_drop_plan(user_text: str) -> bool:
    folded = normalize_entity_alias(user_text)
    if not ac_terms_present(folded) or not ac_pvpc_price_terms_present(folded):
        return False
    if not ac_turn_on_terms_present(folded):
        return False
    return "desde ahora" in folded and any(
        term in folded
        for term in (
            "hasta que baje",
            "hasta que baje el precio",
            "hasta que baje de precio",
            "hasta que este mas barato",
            "hasta que esté mas barato",
        )
    )


def requested_recent_task_correction(user_text: str) -> bool:
    folded = normalize_cancellation_key(user_text)
    return any(
        term in folded
        for term in (
            "te corrijo",
            "corrijo",
            "mejor que",
            "mejor cambia",
            "cambialo",
            "cámbialo",
            "modificalo",
            "modifícalo",
            "en vez de",
        )
    )


def corrected_tts_payload(user_text: str) -> dict[str, Any] | None:
    folded = normalize_cancellation_key(user_text)
    if not any(term in folded for term in ("di ", "diga ", "decir ", "dilo ")):
        return None
    match = re.search(r"(?:diga|di|decir|dilo)\s+(?P<message>.+)$", folded)
    if not match:
        return None
    message = match.group("message").strip(" .")
    message = re.sub(r"^que\s+", "", message).strip()
    if not message:
        return None
    include_time = "hora" in message
    message = re.sub(r"\b(?:y\s+)?la\s+hora\b", "", message).strip(" ,.")
    message = message or "hola"
    return {
        "message": message,
        "include_time": include_time,
        "media_query": "",
    }


def ac_pvpc_price_rows(pvpc: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in (pvpc.get("all_hours") or []) if isinstance(row, dict)]
    usable: list[dict[str, Any]] = []
    for row in rows:
        with contextlib.suppress(Exception):
            usable.append(
                {
                    **row,
                    "hour": int(row["hour"]),
                    "eur_kwh": float(row["eur_kwh"]),
                    "cent_kwh": float(row.get("cent_kwh") or float(row["eur_kwh"]) * 100),
                }
            )
    return sorted(usable, key=lambda item: item["hour"])


def select_ac_pvpc_valley(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    if len(rows) < 2:
        return None
    daytime_rows = [row for row in rows if 8 <= int(row["hour"]) <= 23] or rows
    candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for previous, current in zip(daytime_rows, daytime_rows[1:]):
        delta = float(current["eur_kwh"]) - float(previous["eur_kwh"])
        ratio = float(current["eur_kwh"]) / float(previous["eur_kwh"]) if float(previous["eur_kwh"]) > 0 else 0
        if delta >= 0.03 or ratio >= 1.25:
            candidates.append((delta, previous, current))
    if not candidates:
        return None
    # Choose the first large rise after the cheapest daytime point.
    cheapest = min(daytime_rows, key=lambda row: (float(row["eur_kwh"]), int(row["hour"])))
    later = [item for item in candidates if int(item[1]["hour"]) >= int(cheapest["hour"])]
    delta, previous, current = (later[0] if later else max(candidates, key=lambda item: item[0]))
    start = previous
    threshold = float(previous["eur_kwh"])
    by_hour = {int(row["hour"]): row for row in rows}
    hour = int(previous["hour"]) - 1
    while hour in by_hour and float(by_hour[hour]["eur_kwh"]) <= threshold:
        start = by_hour[hour]
        hour -= 1
    return start, previous, current


def format_ac_pvpc_valley_plan(
    *,
    pvpc: dict[str, Any],
    ac_state: dict[str, Any],
    power_state: dict[str, Any],
) -> str:
    rows = ac_pvpc_price_rows(pvpc)
    selected = select_ac_pvpc_valley(rows)
    if not selected:
        return "No pude detectar una bajada/subida clara del precio kWh hoy para proponer una ventana de AC."
    start, last_cheap, first_expensive = selected
    ac_state_text = str(ac_state.get("state") or "unknown")
    power_value = environment_state_value(power_state, "W")
    start_hour = int(start["hour"])
    end_hour = int(first_expensive["hour"])
    cheap_rows = [row for row in rows if start_hour <= int(row["hour"]) < end_hour]
    slot_lines = "\n".join(
        f"- {row.get('range')}: {float(row['eur_kwh']):.5f} EUR/kWh ({float(row['cent_kwh']):.2f} c/kWh)"
        for row in cheap_rows
    )
    delta_cent = (float(first_expensive["eur_kwh"]) - float(last_cheap["eur_kwh"])) * 100
    return (
        f"Lo que haría es proponer una ventana {start_hour:02d}:00-{end_hour:02d}:00 para el único AC Brokton.\n"
        f"Motivo PVPC: el bloque barato queda antes de la subida de {last_cheap.get('range')} "
        f"({float(last_cheap['cent_kwh']):.2f} c/kWh) a {first_expensive.get('range')} "
        f"({float(first_expensive['cent_kwh']):.2f} c/kWh), subida de {delta_cent:.2f} c/kWh.\n"
        f"Franjas usadas:\n{slot_lines}\n"
        f"Estado actual: input_boolean.brokton_ac_dp1_switch está en {ac_state_text}; "
        f"sensor.powcasa_enchufes_powcasa_enchufes_power marca {power_value[1]}{power_value[2]}.\n"
        "Si me pides agendarlo, crearía dos acciones:\n"
        f"- {start_hour:02d}:00: input_boolean.turn_on sobre input_boolean.brokton_ac_dp1_switch.\n"
        f"- {end_hour:02d}:00: input_boolean.turn_off sobre input_boolean.brokton_ac_dp1_switch.\n"
        "Verificación de encendido: tras el turn_on comprobaría que el input_boolean queda on y que "
        "sensor.powcasa_enchufes_powcasa_enchufes_power sube al menos 300 W. Si no se confirma, haría un único reintento "
        "con ciclo off/on. Si vuelve a fallar, marcaría fallo y enviaría alerta móvil.\n"
        "Verificación de apagado: tras el turn_off comprobaría bajada de al menos 300 W; si no se confirma, haría un único "
        "reintento de apagado. Si falla, marcaría fallo y enviaría alerta móvil.\n"
        "No lo agendo todavía; esta es la explicación para que puedas corregir hora, umbral o política de reintento."
    )


def format_ac_pvpc_shutdown_proposal(
    *,
    pvpc: dict[str, Any],
    ac_state: dict[str, Any],
    power_state: dict[str, Any],
) -> str:
    all_hours = [row for row in (pvpc.get("all_hours") or []) if isinstance(row, dict)]
    now_text = str(pvpc.get("now_local") or "")
    current_hour = None
    with contextlib.suppress(Exception):
        current_hour = dt.datetime.fromisoformat(now_text).hour
    if current_hour is None:
        current_hour = local_now().hour
    future = [row for row in all_hours if int(row.get("hour") or 0) >= current_hour]
    if len(future) < 2:
        return "No puedo proponerte una hora fiable de apagado del AC: no quedan suficientes horas PVPC futuras hoy."

    jumps: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for previous, current in zip(future, future[1:]):
        try:
            previous_price = float(previous["eur_kwh"])
            current_price = float(current["eur_kwh"])
        except (KeyError, TypeError, ValueError):
            continue
        delta = current_price - previous_price
        ratio = current_price / previous_price if previous_price > 0 else 0
        if delta >= 0.03 or ratio >= 1.25:
            jumps.append((delta, previous, current))
    if jumps:
        # Prefer the first meaningful rise; for control decisions it matters when the cheap window ends.
        delta, previous, current = jumps[0]
    else:
        candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for previous, current in zip(future, future[1:]):
            with contextlib.suppress(Exception):
                candidates.append((float(current["eur_kwh"]) - float(previous["eur_kwh"]), previous, current))
        delta, previous, current = max(candidates, key=lambda item: item[0])

    ac_mode = str(ac_state.get("state") or "unknown")
    power_value = environment_state_value(power_state, "W")
    previous_price = float(previous.get("eur_kwh") or 0)
    current_price = float(current.get("eur_kwh") or 0)
    delta_cent = (current_price - previous_price) * 100
    next_cheaper = [
        row for row in future
        if int(row.get("hour") or 0) > int(current.get("hour") or 0)
        and float(row.get("eur_kwh") or 999) <= previous_price
    ]
    resume_text = ""
    if next_cheaper:
        resume_text = f" La siguiente franja igual o más barata aparece en {next_cheaper[0].get('range')}."
    return (
        f"Propondría apagar el aire acondicionado a las {current.get('start') or str(current.get('hour')).zfill(2) + ':00'}.\n"
        f"Motivo: justo antes, {previous.get('range')} cuesta {previous_price:.5f} EUR/kWh "
        f"({previous_price * 100:.2f} c/kWh), y después {current.get('range')} sube a {current_price:.5f} EUR/kWh "
        f"({current_price * 100:.2f} c/kWh), subida de {delta_cent:.2f} c/kWh.\n"
        f"Estado actual: input_boolean.brokton_ac_dp1_switch está en {ac_mode}; {power_state.get('entity_id', 'sensor.powcasa_enchufes_powcasa_enchufes_power')} marca {power_value[1]}{power_value[2]}.\n"
        f"Si lo agendamos, usaría input_boolean.turn_off sobre input_boolean.brokton_ac_dp1_switch y verificaría una bajada mínima de 300 W en sensor.powcasa_enchufes_powcasa_enchufes_power.{resume_text}\n"
        "No lo agendo todavía; es una recomendación para que puedas corregir la hora o pedirme que cree la tarea."
    )


def format_ac_until_price_drop_plan(
    *,
    pvpc: dict[str, Any],
    ac_state: dict[str, Any],
    power_state: dict[str, Any],
) -> str:
    rows = ac_pvpc_price_rows(pvpc)
    now_text = str(pvpc.get("now_local") or "")
    current_hour = None
    with contextlib.suppress(Exception):
        current_hour = dt.datetime.fromisoformat(now_text).hour
    if current_hour is None:
        current_hour = local_now().hour
    current_row = next((row for row in rows if int(row["hour"]) == int(current_hour)), None)
    if current_row is None:
        return "No puedo preparar ese control del AC: no encuentro el precio PVPC de la hora actual."
    current_price = float(current_row["eur_kwh"])
    future = [row for row in rows if int(row["hour"]) > int(current_hour)]
    cheaper = [row for row in future if float(row["eur_kwh"]) < current_price]
    ac_mode = str(ac_state.get("state") or "unknown")
    power_value = environment_state_value(power_state, "W")
    if cheaper:
        stop_row = cheaper[0]
        stop_text = f"{stop_row.get('start') or str(stop_row['hour']).zfill(2) + ':00'}"
        stop_reason = (
            f"la primera franja posterior más barata que la actual es {stop_row.get('range')} "
            f"({float(stop_row['cent_kwh']):.2f} c/kWh), frente a la hora actual "
            f"{current_row.get('range')} ({float(current_row['cent_kwh']):.2f} c/kWh)."
        )
    else:
        stop_text = "sin hora hoy"
        stop_reason = (
            f"no veo ninguna franja posterior hoy más barata que la actual "
            f"{current_row.get('range')} ({float(current_row['cent_kwh']):.2f} c/kWh)."
        )
    return (
        "Entiendo la orden como: encender el único AC Brokton ahora y apagarlo cuando el precio del kWh baje "
        "respecto al precio actual.\n"
        f"Estado actual: input_boolean.brokton_ac_dp1_switch está en {ac_mode}; "
        f"sensor.powcasa_enchufes_powcasa_enchufes_power marca {power_value[1]}{power_value[2]}.\n"
        f"PVPC: {stop_reason}\n"
        "Lo que haría, si me confirmas que lo ejecute/agende:\n"
        "- Ahora: input_boolean.turn_on sobre input_boolean.brokton_ac_dp1_switch.\n"
        f"- Apagado: input_boolean.turn_off cuando llegue {stop_text}, si existe esa bajada hoy.\n"
        "Verificación: tras encender/apagar comprobaría el input_boolean y una variación de unos 300 W en "
        "sensor.powcasa_enchufes_powcasa_enchufes_power, con un único reintento si falla.\n"
        "No lo ejecuto todavía: esta frase mezcla acción inmediata y condición futura, así que primero dejo el plan para que lo corrijas."
    )


def scheduling_intent_hint(user_text: str) -> str:
    folded = fold_accents(user_text.lower())
    if not folded.strip():
        return ""

    action = r"(?:consulta|mira|comprueba|enciende|apaga|activa|desactiva|avisa|manda|notifica|pulsa|abre|cierra|ejecuta|toca|reproduce)"
    weekday = r"(?:lunes|martes|miercoles|jueves|viernes|sabado|domingo)"
    relative_day = r"(?:hoy|manana|pasado manana|esta tarde|esta noche|esta madrugada)"
    calendar_time = rf"(?:(?:(?:el\s+)?(?:proximo|siguiente)\s+|el\s+)?{weekday}|{relative_day})"
    future_patterns = [
        r"\b(?:agenda|programa|reprograma|planifica|recuerdame|avisame|mandame|notificame)\b",
        r"\b(?:dentro de|en|al cabo de|pasados?|tras|despues de|despues|mas tarde|luego)\s+\d+\s*(?:segundos?|mins?|minutos?|horas?|dias?)\b",
        r"\b(?:a las|a la|sobre las|para las|hacia las)\s+\d{1,2}(?::\d{2})?\b",
        rf"\b{calendar_time}\b[^.!?]*\b{action}\b",
        rf"\b{action}\b[^.!?]*\b{calendar_time}\b",
        r"\b(?:cuando|en cuanto|si)\b.+\b(?:enciende|apaga|activa|desactiva|avisa|manda|notifica|pulsa|abre|cierra|ejecuta)\b",
        r"\b(?:cada|todos los|todas las|diariamente|semanalmente|mensualmente|periodicamente)\b",
        r"\b(?:hasta que|hasta nueva orden|hasta que yo cancele|hasta que cancele|hasta que diga|mientras)\b",
        r"\b(?:durante|por)\s+\d+\s*(?:segundos?|mins?|minutos?|horas?)\b",
    ]
    matched = [pattern for pattern in future_patterns if re.search(pattern, folded)]
    if not matched:
        return ""

    return (
        "\nDeteccion temporal de esta peticion:\n"
        "- La peticion del usuario contiene lenguaje de futuro, demora, condicion, intervalo o recurrencia.\n"
        "- Debes usar schedule_task antes de prometer o ejecutar la accion futura. No ejecutes ahora acciones de Home Assistant que el usuario pidio para despues/cuando/cada cierto tiempo.\n"
        "- Calcula run_at como ISO 8601 usando la fecha/hora local actual indicada arriba; si es 'dentro de/en N minutos', suma ese retraso.\n"
        "- Sinonimos temporales que implican agendar: agenda, programa, planifica, recuerdame, avisame, mandame, notificame, dentro de, en N minutos/horas, al cabo de, pasados, tras, despues, luego, mas tarde, a las, para las, manana, esta tarde/noche, cuando, en cuanto, si se cumple, cada, todos los, hasta que cancele/hasta nueva orden.\n"
        "- Si hay una secuencia futura con esperas cortas ('en 1 minuto haz A, espera 2 segundos y haz B'), crea una sola tarea cuya instruction incluya wait_seconds para la espera corta interna.\n"
        "- Si hay un intervalo largo ('enciende a las 22:00 y apaga a las 03:00' o 'durante 2 horas'), crea tarea de inicio y tarea de fin, salvo sirenas pulsables donde corresponde ha_press_entity_interval cuando llegue la tarea.\n"
        "- Si es recurrente ('cada hora/dia', 'todos los...', 'hasta que cancele'), usa interval_seconds y deja la tarea activa hasta cancelacion.\n"
    )


def completion_notification_requested(user_text: str) -> bool:
    folded = normalize_cancellation_key(user_text)
    patterns = (
        r"\bavisa(?:me)?\b[^.!?]{0,80}\b(?:acab|termin|finaliz|complet|list)",
        r"\bnotifica(?:me)?\b[^.!?]{0,80}\b(?:acab|termin|finaliz|complet|list)",
        r"\bconfirma(?:me)?\b[^.!?]{0,80}\b(?:acab|termin|finaliz|complet|hecho)",
        r"\b(?:cuando|al)\s+(?:acab|termin|finaliz|complet)",
        r"\baviso\s+(?:al\s+)?(?:terminar|finalizar|completar)",
        r"\bha_send_mobile_alert\b",
    )
    return any(re.search(pattern, folded) for pattern in patterns)


def mobile_notification_explicitly_requested(user_text: str) -> bool:
    folded = normalize_cancellation_key(user_text)
    if "ha_send_mobile_alert" in folded:
        return True
    channel = bool(
        re.search(
            r"\b(?:movil|telefono|notificacion movil|aviso movil|alerta movil|"
            r"mobile_app|push al movil|push al telefono)\b",
            folded,
        )
    )
    request = bool(
        re.search(
            r"\b(?:avisa|avisame|notifica|notificame|manda|mandame|envia|enviame|"
            r"alerta|aviso|notificacion|confirma|confirmame|push)\b",
            folded,
        )
    )
    return channel and request


def notification_preference_declined(user_text: str) -> bool:
    folded = normalize_cancellation_key(user_text)
    return any(
        phrase in folded
        for phrase in (
            "no me avises",
            "sin alerta",
            "sin aviso",
            "no hace falta avisar",
            "solo confirma aqui",
            "solo confirmacion en el chat",
        )
    )


def notification_preference_reply(user_text: str) -> bool | None:
    folded = normalize_cancellation_key(user_text)
    if notification_preference_declined(folded) or re.fullmatch(r"(?:no|no gracias)", folded):
        return False
    if re.fullmatch(r"(?:si|si gracias|vale|de acuerdo|ambos)", folded):
        return True
    if any(term in folded for term in ("alerta", "avisame", "notificame", "al movil")):
        return True
    return None


def critical_operation_category(user_text: str) -> str | None:
    folded = normalize_cancellation_key(user_text)
    security_terms = (
        "alarma", "sirena", "seguridad", "cerradura", "la cancela",
        "abrir cancela", "cerrar cancela", "camara",
        "intruso", "ladron", "puerta", "timbre",
    )
    electricity_terms = (
        "electricidad", "corriente", "potencia", "consumo", "kwh", "kw h",
        "icp", "pia", "suministro electrico", "corte de luz", "termo",
    )
    if any(term in folded for term in security_terms):
        return "seguridad"
    if any(term in folded for term in WATER_TERMS):
        return "agua"
    if any(term in folded for term in electricity_terms):
        return "electricidad"
    return None


def critical_action_requested(user_text: str) -> bool:
    folded = normalize_cancellation_key(user_text)
    advisory_terms = (
        "que propones", "me propones", "que hora me propones", "recomienda",
        "recomiendas", "sugiere", "sugerencia", "que harias", "qué harías",
        "que entidad usarias", "que entidad usarias", "que servicio usarias",
        "no ejecutes", "no ejecutar", "no realices", "sin ejecutar",
    )
    hard_action_terms = ("agenda", "programa", "planifica", "ejecuta", "hazlo", "crea la tarea")
    if any(term in folded for term in advisory_terms) and not any(term in folded for term in hard_action_terms):
        return False
    action_pattern = (
        r"\b(?:enciende|encender|apaga|apagar|activa|activar|desactiva|desactivar|"
        r"pon|poner|prende|prender|abre|abrir|cierra|cerrar|pulsa|pulsar|corta|cortar|reanuda|rearmar|"
        r"agenda|programa|planifica|ejecuta|manda|enviar)\b"
    )
    return bool(re.search(action_pattern, folded))


def should_offer_critical_completion_alert(user_text: str) -> str | None:
    # Mobile alerts are opt-in. Criticality must never create or propose a phone alert implicitly.
    return None


def attach_completion_notification(plan: dict[str, Any], user_text: str) -> dict[str, Any]:
    if notification_preference_declined(user_text):
        enriched = dict(plan)
        enriched["completion_notification"] = {"enabled": False}
        return enriched
    if not mobile_notification_explicitly_requested(user_text):
        return plan
    enriched = dict(plan)
    enriched["completion_notification"] = {
        "enabled": True,
        "title": "Codexon",
        "message": f"Tarea completada: {str(plan.get('name') or 'automatizacion')}",
        "notify": "notify/mobile_app_sm_a566b",
        "volume": 100,
        "media_stream": "alarm_stream",
        "speak": True,
        "critical": True,
    }
    return enriched


def parse_fs_roots(value: str) -> list[Path]:
    roots: list[Path] = []
    for item in value.split(","):
        text = item.strip()
        if not text:
            continue
        roots.append(Path(text).expanduser().resolve())
    return roots or [Path.cwd().resolve()]


async def fetch_openrouter_model_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    if httpx is None:
        return catalog
    try:
        headers = {}
        api_key = os.getenv("OPENROUTER_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
            response.raise_for_status()
            models = response.json().get("data", [])
    except Exception:
        return catalog
    for model in models:
        pricing = model.get("pricing") or {}
        model_id = model.get("id")
        if not model_id:
            continue
        try:
            input_price = float(pricing.get("prompt", 0))
            output_price = float(pricing.get("completion", 0))
        except (TypeError, ValueError):
            input_price = 0.0
            output_price = 0.0
        params = set(model.get("supported_parameters") or [])
        architecture = model.get("architecture") or {}
        output_modalities = architecture.get("output_modalities") or []
        catalog[model_id] = {
            "id": model_id,
            "name": model.get("name"),
            "context_length": model.get("context_length"),
            "input_price": input_price,
            "output_price": output_price,
            "input_price_per_million": input_price * 1_000_000,
            "output_price_per_million": output_price * 1_000_000,
            "supports_tools": "tools" in params or "tool_choice" in params,
            "supports_chat": not output_modalities or "text" in output_modalities,
            "output_modalities": list(output_modalities),
            "supports_structured_outputs": "structured_outputs" in params,
            "supported_parameters": sorted(params),
        }
    return catalog


def extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    elif not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


@dataclasses.dataclass(frozen=True)
class SemanticStatisticalPlan:
    metric: str
    semantic_query: str
    aggregation: str
    scope: str
    period_status: str
    period_text: str
    period_kind: str = "missing"
    start_time: str = ""
    end_time: str = ""
    rolling_days: int = 0
    clarification_question: str = ""
    needs_activity_attribution: bool = False
    activity_scope: str = ""


def parse_semantic_statistical_plan(raw: str) -> SemanticStatisticalPlan | None:
    try:
        payload = extract_json_object(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if str(payload.get("kind") or "").strip().lower() != "statistical":
        return None
    metric = str(payload.get("metric") or "").strip()
    aggregation = str(payload.get("aggregation") or "").strip().lower()
    scope = str(payload.get("scope") or "").strip()
    period_status = str(payload.get("period_status") or "missing").strip().lower()
    period_text = str(payload.get("period_text") or "").strip()
    if not metric or not aggregation or aggregation not in {
        "maximum", "minimum", "average", "median", "standard_deviation",
        "trend", "anomaly", "sum", "range", "count", "comparison"
    }:
        return None
    if period_status not in {"explicit", "missing"}:
        period_status = "missing"
    period_kind = str(payload.get("period_kind") or period_status).strip().lower()
    if period_kind not in {
        "missing", "today", "yesterday", "current_week", "previous_week",
        "current_month", "previous_month", "rolling_days", "explicit_dates",
        "all_history",
    }:
        period_kind = "missing" if period_status == "missing" else "explicit_dates"
    try:
        rolling_days = max(0, int(payload.get("rolling_days") or 0))
    except (TypeError, ValueError):
        rolling_days = 0
    return SemanticStatisticalPlan(
        metric=metric,
        semantic_query=str(payload.get("semantic_query") or metric).strip(),
        aggregation=aggregation,
        scope=scope,
        period_status=period_status,
        period_text=period_text,
        period_kind=period_kind,
        start_time=str(payload.get("start_time") or "").strip(),
        end_time=str(payload.get("end_time") or "").strip(),
        rolling_days=rolling_days,
        clarification_question=str(payload.get("clarification_question") or "").strip(),
        needs_activity_attribution=bool(payload.get("needs_activity_attribution")),
        activity_scope=str(payload.get("activity_scope") or "").strip(),
    )


def canonical_statistical_period(
    plan: SemanticStatisticalPlan,
    *,
    now: dt.datetime | None = None,
) -> tuple[str, str] | None:
    reference = (now or local_now()).astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    day_start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    if plan.period_kind == "today":
        start, end = day_start, reference
    elif plan.period_kind == "yesterday":
        start, end = day_start - dt.timedelta(days=1), day_start
    elif plan.period_kind == "current_week":
        start = (day_start - dt.timedelta(days=reference.weekday()))
        end = reference
    elif plan.period_kind == "previous_week":
        end = day_start - dt.timedelta(days=reference.weekday())
        start = end - dt.timedelta(days=7)
    elif plan.period_kind == "current_month":
        start, end = day_start.replace(day=1), reference
    elif plan.period_kind == "previous_month":
        end = day_start.replace(day=1)
        start = (end - dt.timedelta(days=1)).replace(day=1)
    elif plan.period_kind == "rolling_days" and plan.rolling_days > 0:
        start, end = reference - dt.timedelta(days=plan.rolling_days), reference
    elif plan.period_kind == "explicit_dates" and plan.start_time:
        try:
            start = dt.datetime.fromisoformat(plan.start_time)
            end = dt.datetime.fromisoformat(plan.end_time) if plan.end_time else reference
        except ValueError:
            return None
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
        if end.tzinfo is None:
            end = end.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
    else:
        return None
    return (
        start.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).isoformat(timespec="seconds"),
        end.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).isoformat(timespec="seconds"),
    )


def statistical_answer_is_complete(answer: str) -> bool:
    folded = normalize_entity_alias(answer)
    without_entity_ids = re.sub(r"\b[a-z_]+\.[a-z0-9_.]+\b", " ", folded)
    if re.search(r"(?<!\w)-?\d+(?:[,.]\d+)?", without_entity_ids):
        return True
    return any(
        phrase in folded
        for phrase in (
            "no hay datos",
            "sin datos",
            "no se encontraron datos",
            "no pude calcular",
            "no he podido calcular",
            "cobertura insuficiente",
        )
    )


def statistical_answer_period_is_consistent(
    answer: str,
    period: tuple[str, str] | None,
) -> bool:
    if period is None:
        return True
    try:
        start = dt.datetime.fromisoformat(period[0]).date()
        end = dt.datetime.fromisoformat(period[1]).date()
    except ValueError:
        return False
    folded = normalize_entity_alias(answer)
    mentioned: list[dt.date] = []
    month_pattern = "|".join(SPANISH_MONTH_NAMES)
    for match in re.finditer(
        rf"\b(?P<day>\d{{1,2}})\s+de\s+(?P<month>{month_pattern})"
        rf"(?:\s+de\s+(?P<year>\d{{4}}))?\b",
        folded,
    ):
        month = SPANISH_MONTH_NAMES.index(match.group("month")) + 1
        year = int(match.group("year") or end.year)
        if not match.group("year") and month > end.month + 6:
            year -= 1
        with contextlib.suppress(ValueError):
            mentioned.append(dt.date(year, month, int(match.group("day"))))
    for match in re.finditer(
        r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b",
        folded,
    ):
        with contextlib.suppress(ValueError):
            mentioned.append(
                dt.date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
    return all(start <= value <= end for value in mentioned)


def extract_statistical_escalation(answer: str) -> tuple[str, str | None]:
    pattern = re.compile(
        r"(?im)^\s*(?:[-*]\s*)?ESCALATE_STATISTICS\s*:\s*(?P<reason>.+?)\s*$"
    )
    match = pattern.search(answer or "")
    if match is None:
        return (answer or "").strip(), None
    cleaned = pattern.sub("", answer or "").strip()
    return cleaned, match.group("reason").strip()


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif content is not None:
            chars += len(str(content))
    return max(1, chars // 4)


@dataclasses.dataclass
class ModelRequest:
    task: str
    prompt_tokens_estimate: int
    requires_tools: bool = False
    requires_memory: bool = False
    priority: str = "cost"
    max_budget_usd: float | None = None
    preferred_model: str | None = None


@dataclasses.dataclass
class ModelSelection:
    model: str
    reason: str
    estimated_cost_usd: float | None
    fallbacks: list[str]


MODEL_PREFERENCE_SETTING_BY_TASK = {
    "classification": "classification_model",
    "statistical_planning": "statistical_planning_model",
    "statistical_reasoning": "statistical_reasoning_model",
}

ECONOMY_MODEL = "google/gemini-2.5-flash-lite"
BUDGET_STAGE_DEFAULTS = {
    "classification": (450, 180, 1),
    "statistical_planning": (550, 220, 1),
    "homeassistant": (22000, 600, 2),
    "memory_extraction": (450, 320, 1),
}


class ModelRouter:
    def __init__(self, config_path: Path, model_catalog: dict[str, dict[str, Any]]) -> None:
        self.config_path = config_path
        self.model_catalog = model_catalog
        self.config = self._load_config(config_path)

    def _load_config(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"default": DEFAULT_MODEL, "routes": {}}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if yaml is not None else None
        if not isinstance(data, dict):
            return {"default": DEFAULT_MODEL, "routes": {}}
        data.setdefault("routes", {})
        return data

    def select(self, request: ModelRequest) -> ModelSelection:
        routes = self.config.get("routes") or {}
        route = routes.get(request.task) or {}
        candidates = self._candidate_models(request, route)
        chosen = candidates[0]
        reason_parts = []
        if request.preferred_model:
            reason_parts.append(f"modelo preferido por usuario: {request.preferred_model}")
        elif route.get("model"):
            reason_parts.append(f"ruta '{request.task}' -> {route['model']}")
        else:
            reason_parts.append(f"default -> {chosen}")
        if route.get("reason"):
            reason_parts.append(str(route["reason"]))
        if request.requires_tools:
            reason_parts.append("requiere tools")
        if request.max_budget_usd is not None:
            reason_parts.append(f"presupuesto max ${request.max_budget_usd:.6f}")

        return ModelSelection(
            model=chosen,
            reason="; ".join(reason_parts),
            estimated_cost_usd=self.estimate_cost(chosen, request.prompt_tokens_estimate, 600),
            fallbacks=[model for model in candidates[1:] if model != chosen],
        )

    def _candidate_models(self, request: ModelRequest, route: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for model in (
            request.preferred_model,
            route.get("model"),
            *route.get("fallbacks", []),
            *self.config.get("fallbacks", []),
            self.config.get("default"),
            FALLBACK_MODEL,
        ):
            if isinstance(model, str) and model and model not in candidates:
                candidates.append(model)

        filtered = [
            model
            for model in candidates
            if self._allowed(model, request, ignore_priority=model == request.preferred_model)
        ]
        return filtered or candidates or [FALLBACK_MODEL]

    def _allowed(self, model: str, request: ModelRequest, *, ignore_priority: bool = False) -> bool:
        meta = self.model_catalog.get(model, {})
        if request.requires_tools and meta and not meta.get("supports_tools", False):
            return False
        if request.max_budget_usd is not None:
            estimated = self.estimate_cost(model, request.prompt_tokens_estimate, 600)
            if estimated is not None and estimated > request.max_budget_usd:
                return False
        priority = {} if ignore_priority else self.config.get("priorities", {}).get(request.priority, {})
        max_in = priority.get("max_input_price_per_million")
        max_out = priority.get("max_output_price_per_million")
        if meta and max_in is not None and meta.get("input_price_per_million", 0) > float(max_in):
            return False
        if meta and max_out is not None and meta.get("output_price_per_million", 0) > float(max_out):
            return False
        return True

    def configured_models(self) -> list[str]:
        models: list[str] = []
        routes = self.config.get("routes") or {}
        values: list[Any] = [
            self.config.get("default"),
            *(self.config.get("fallbacks") or []),
        ]
        for route in routes.values():
            if not isinstance(route, dict):
                continue
            values.extend([route.get("model"), *(route.get("fallbacks") or [])])
        for model in values:
            if isinstance(model, str) and model and model not in models:
                models.append(model)
        return models

    def search_models(self, query: str, limit: int = 20) -> list[str]:
        folded = fold_accents(query.lower())
        matches = [
            model_id
            for model_id, meta in self.model_catalog.items()
            if meta.get("supports_tools") and meta.get("supports_chat", True)
            if folded in fold_accents(f"{model_id} {meta.get('name') or ''}".lower())
        ]
        matches.sort(
            key=lambda model_id: (
                not bool(self.model_catalog[model_id].get("supports_tools")),
                float(self.model_catalog[model_id].get("input_price_per_million") or 0)
                + float(self.model_catalog[model_id].get("output_price_per_million") or 0),
                model_id,
            )
        )
        return matches[: max(1, limit)]

    def selectable_models(self) -> list[str]:
        configured = self.configured_models()
        if not self.model_catalog:
            return configured
        compatible = {
            model_id
            for model_id, meta in self.model_catalog.items()
            if meta.get("supports_tools") and meta.get("supports_chat", True)
        }
        configured = [model_id for model_id in configured if model_id in compatible]
        official = sorted(model_id for model_id in compatible if model_id not in configured)
        return [*configured, *official]

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        meta = self.model_catalog.get(model)
        if not meta:
            return None
        input_price = float(meta["input_price"])
        output_price = float(meta["output_price"])
        if input_price < 0 or output_price < 0:
            return None
        return (input_tokens * input_price) + (output_tokens * output_price)

    def price_tuple(self, model: str) -> tuple[float, float] | None:
        meta = self.model_catalog.get(model)
        if not meta:
            return None
        input_price = float(meta["input_price"])
        output_price = float(meta["output_price"])
        if input_price < 0 or output_price < 0:
            return None
        return input_price, output_price

    def describe(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "default": self.config.get("default"),
            "routes": self.config.get("routes", {}),
            "fallbacks": self.config.get("fallbacks", []),
        }


def compact_json(value: Any, max_chars: int = 6000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncado]"
    return text


def safe_tool_name(name: str) -> str:
    fixed = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    if not re.match(r"^[a-zA-Z_]", fixed):
        fixed = f"tool_{fixed}"
    return fixed[:64]


def tool_result_to_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        if hasattr(item, "text"):
            parts.append(item.text)
        elif hasattr(item, "model_dump"):
            parts.append(compact_json(item.model_dump()))
        else:
            parts.append(str(item))
    return "\n".join(parts).strip()


def exception_summary(exc: BaseException) -> str:
    if isinstance(exc, ExceptionGroup):
        messages: list[str] = []
        for sub_exc in exc.exceptions:
            messages.append(exception_summary(sub_exc))
        return "; ".join(message for message in messages if message) or str(exc)
    return f"{exc.__class__.__name__}: {exc}"


def derive_ha_base_url(mcp_url: str | None) -> str | None:
    if not mcp_url:
        return None
    for suffix in ("/api/mcp", "/mcp"):
        if mcp_url.endswith(suffix):
            return mcp_url[: -len(suffix)]
    return mcp_url.rstrip("/")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                topic TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                summary TEXT NOT NULL,
                raw TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                raw TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                run_at TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                instruction TEXT NOT NULL,
                result TEXT,
                last_error TEXT,
                priority INTEGER NOT NULL DEFAULT 50,
                interval_seconds INTEGER,
                cancellation_key TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                retry_backoff_seconds INTEGER NOT NULL DEFAULT 30,
                lease_owner TEXT,
                lease_expires_at TEXT,
                last_started_at TEXT,
                recovery_policy TEXT NOT NULL DEFAULT 'retry'
            );

            CREATE TABLE IF NOT EXISTS task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                worker_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                estimated_cost_usd REAL,
                context TEXT,
                provider TEXT,
                duration_ms INTEGER,
                router_reason TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memories_topic ON memories(topic);
            CREATE INDEX IF NOT EXISTS idx_observations_created ON observations(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_run_at ON tasks(status, run_at);
            CREATE INDEX IF NOT EXISTS idx_task_runs_task_started ON task_runs(task_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS entity_catalog (
                entity_id TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                domain TEXT NOT NULL,
                friendly_name TEXT,
                area_name TEXT,
                device_name TEXT,
                device_class TEXT,
                unit TEXT,
                aliases TEXT,
                state TEXT,
                last_changed TEXT,
                last_updated TEXT,
                source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entity_resolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                query TEXT NOT NULL,
                domain TEXT,
                resolved_entity_id TEXT NOT NULL,
                friendly_name TEXT,
                score INTEGER,
                candidates_json TEXT,
                source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entity_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                normalized_alias TEXT NOT NULL,
                domain TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 50,
                confidence REAL NOT NULL DEFAULT 0.7,
                source TEXT NOT NULL,
                notes TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(entity_id, normalized_alias)
            );

            CREATE TABLE IF NOT EXISTS entity_roles (
                entity_id TEXT NOT NULL,
                role TEXT NOT NULL,
                criticality TEXT NOT NULL DEFAULT 'normal',
                intent_hint TEXT,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(entity_id, role)
            );

            CREATE TABLE IF NOT EXISTS entity_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                location_alias TEXT NOT NULL,
                normalized_location TEXT NOT NULL,
                room TEXT,
                zone TEXT,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(entity_id, normalized_location)
            );

            CREATE TABLE IF NOT EXISTS entity_teachings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teaching_type TEXT NOT NULL,
                key_text TEXT NOT NULL,
                normalized_key TEXT NOT NULL,
                target_entity_id TEXT NOT NULL,
                source TEXT NOT NULL,
                notes TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(teaching_type, normalized_key)
            );

            CREATE INDEX IF NOT EXISTS idx_entity_catalog_domain ON entity_catalog(domain);
            CREATE INDEX IF NOT EXISTS idx_entity_resolutions_query ON entity_resolutions(query);
            CREATE INDEX IF NOT EXISTS idx_entity_aliases_normalized ON entity_aliases(normalized_alias);
            CREATE INDEX IF NOT EXISTS idx_entity_aliases_domain ON entity_aliases(domain);
            CREATE INDEX IF NOT EXISTS idx_entity_roles_role ON entity_roles(role);
            CREATE INDEX IF NOT EXISTS idx_entity_locations_normalized ON entity_locations(normalized_location);
            CREATE INDEX IF NOT EXISTS idx_entity_teachings_lookup
            ON entity_teachings(teaching_type, normalized_key, active);
            CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at);
            """
        )
        self._ensure_usage_columns()
        self._ensure_task_columns()
        self._ensure_entity_alias_tables()
        ensure_event_schema(self.conn)
        self.conn.commit()

    def _ensure_usage_columns(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(usage_events)")
        }
        additions = {
            "provider": "TEXT",
            "duration_ms": "INTEGER",
            "router_reason": "TEXT",
        }
        for column, column_type in additions.items():
            if column not in columns:
                self.conn.execute(f"ALTER TABLE usage_events ADD COLUMN {column} {column_type}")

    def close(self) -> None:
        self.conn.close()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.conn.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )
        self.conn.commit()

    def add_memory(
        self,
        *,
        kind: str,
        topic: str,
        content: str,
        confidence: float = 0.7,
        source: str,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO memories(created_at, updated_at, kind, topic, content, confidence, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now, now, kind[:40], topic[:120], content.strip(), confidence, source[:80]),
        )
        self.conn.commit()

    def add_observation(self, *, source: str, summary: str, raw: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO observations(created_at, source, summary, raw) VALUES (?, ?, ?, ?)",
            (utc_now(), source[:80], summary.strip(), raw),
        )
        self.conn.commit()

    def add_event(self, *, level: str, message: str, raw: str | None = None) -> None:
        runtime_log(level, "memory.event", message, raw=raw)
        self.conn.execute(
            "INSERT INTO events(created_at, level, message, raw) VALUES (?, ?, ?, ?)",
            (utc_now(), level[:20], message.strip(), raw),
        )
        self.conn.commit()

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT created_at, level, message, raw
                FROM events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def _ensure_task_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        additions = {
            "priority": "INTEGER NOT NULL DEFAULT 50",
            "interval_seconds": "INTEGER",
            "cancellation_key": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": f"INTEGER NOT NULL DEFAULT {DEFAULT_TASK_MAX_ATTEMPTS}",
            "retry_backoff_seconds": f"INTEGER NOT NULL DEFAULT {DEFAULT_TASK_RETRY_BACKOFF_SECONDS}",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
            "last_started_at": "TEXT",
            "recovery_policy": "TEXT NOT NULL DEFAULT 'retry'",
        }
        for column, definition in additions.items():
            if column not in columns:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_cancellation_key ON tasks(cancellation_key)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_dispatch ON tasks(status, priority DESC, run_at ASC)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                worker_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_runs_task_started ON task_runs(task_id, started_at DESC)"
        )
        for row in self.conn.execute("SELECT id, instruction, recovery_policy FROM tasks"):
            policy = task_recovery_policy(str(row["instruction"] or ""))
            if str(row["recovery_policy"] or "") != policy:
                self.conn.execute(
                    "UPDATE tasks SET recovery_policy = ? WHERE id = ?",
                    (policy, int(row["id"])),
                )

    def _ensure_entity_alias_tables(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(entity_aliases)")
        }
        additions = {
            "notes": "TEXT",
        }
        for column, definition in additions.items():
            if column not in columns:
                self.conn.execute(f"ALTER TABLE entity_aliases ADD COLUMN {column} {definition}")
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_aliases_entity_alias
            ON entity_aliases(entity_id, normalized_alias)
            """
        )
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_locations_entity_location
            ON entity_locations(entity_id, normalized_location)
            """
        )
        self.migrate_entity_aliases_from_catalog()

    def upsert_entity_alias(
        self,
        *,
        entity_id: str,
        alias: str,
        domain: str,
        priority: int = 50,
        confidence: float = 0.7,
        source: str = "catalog",
        notes: str | None = None,
    ) -> None:
        alias = alias.strip()
        normalized = normalize_entity_alias(alias)
        if not entity_id or not alias or not normalized:
            return
        self.conn.execute(
            """
            INSERT INTO entity_aliases(
                entity_id, alias, normalized_alias, domain, priority, confidence, source, notes, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id, normalized_alias) DO UPDATE SET
                alias=excluded.alias,
                domain=excluded.domain,
                priority=MAX(entity_aliases.priority, excluded.priority),
                confidence=MAX(entity_aliases.confidence, excluded.confidence),
                source=excluded.source,
                notes=COALESCE(excluded.notes, entity_aliases.notes),
                updated_at=excluded.updated_at
            """,
            (
                entity_id,
                alias,
                normalized,
                domain,
                int(priority),
                float(confidence),
                source[:80],
                notes,
                utc_now(),
            ),
        )

    def upsert_entity_role(
        self,
        *,
        entity_id: str,
        role: str,
        criticality: str = "normal",
        intent_hint: str | None = None,
        source: str = "catalog",
    ) -> None:
        if not entity_id or not role:
            return
        self.conn.execute(
            """
            INSERT INTO entity_roles(entity_id, role, criticality, intent_hint, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id, role) DO UPDATE SET
                criticality=excluded.criticality,
                intent_hint=COALESCE(excluded.intent_hint, entity_roles.intent_hint),
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (entity_id, role, criticality, intent_hint, source[:80], utc_now()),
        )

    def upsert_entity_location(
        self,
        *,
        entity_id: str,
        location_alias: str,
        room: str | None = None,
        zone: str | None = None,
        source: str = "catalog",
    ) -> None:
        location_alias = location_alias.strip()
        normalized = normalize_entity_alias(location_alias)
        if not entity_id or not normalized:
            return
        self.conn.execute(
            """
            INSERT INTO entity_locations(
                entity_id, location_alias, normalized_location, room, zone, source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id, normalized_location) DO UPDATE SET
                location_alias=excluded.location_alias,
                room=COALESCE(excluded.room, entity_locations.room),
                zone=COALESCE(excluded.zone, entity_locations.zone),
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (entity_id, location_alias, normalized, room, zone, source[:80], utc_now()),
        )

    def infer_entity_roles(self, row: sqlite3.Row | dict[str, Any], aliases: list[str]) -> list[tuple[str, str, str]]:
        entity_id = str(row["entity_id"] if isinstance(row, sqlite3.Row) else row.get("entity_id") or "")
        domain = str(row["domain"] if isinstance(row, sqlite3.Row) else row.get("domain") or entity_id.split(".", 1)[0])
        device_class = str(row["device_class"] if isinstance(row, sqlite3.Row) else row.get("device_class") or "").lower()
        unit = str(row["unit"] if isinstance(row, sqlite3.Row) else row.get("unit") or "").lower()
        text = normalize_entity_alias(
            " ".join(
                str(part or "")
                for part in (
                    entity_id,
                    row["friendly_name"] if isinstance(row, sqlite3.Row) else row.get("friendly_name"),
                    row["area_name"] if isinstance(row, sqlite3.Row) else row.get("area_name"),
                    row["device_name"] if isinstance(row, sqlite3.Row) else row.get("device_name"),
                    device_class,
                    unit,
                    " ".join(aliases),
                )
            )
        )
        roles: list[tuple[str, str, str]] = []
        if domain == "cover":
            roles.append(("cover_blind", "normal", "abrir/cerrar/subir/bajar persianas y estores"))
        if (
            entity_id in {
                "input_boolean.brokton_ac_dp1_switch",
                "input_boolean.brokton_ac_dp8_eco",
                "input_select.brokton_ac_dp4_mode",
            }
            or (domain == "climate" and "brokton" in text)
        ):
            roles.append(("air_conditioner", "electricity", "controlar el unico aire acondicionado Brokton"))
        if domain in {"light", "switch"} and any(token in text for token in ("luz", "luces", "rele", "relay")):
            roles.append(("physical_light", "normal", "encender/apagar luces fisicas"))
        if domain == "sensor" and (device_class == "illuminance" or unit in {"lx", "lux"} or "luminosidad" in text or "sensor_luz" in text):
            roles.append(("luminosity_sensor", "normal", "cuanta luz, lux o luminosidad"))
        if entity_id in {"sensor.myups_status", "sensor.myups_status_data"} or "ups" in text:
            roles.append(("power_supply_status", "electricity", "se ha ido la luz o estado del suministro electrico"))
        if domain == "button" and "sirena" in text:
            roles.append(("siren", "security", "activar sirena o avisador"))
        roles.extend(infer_water_entity_roles(text, domain=domain, device_class=device_class, unit=unit))
        if "nevera" in text:
            roles.append(("fridge", "electricity", "nevera y conservacion de frio"))
        if domain == "binary_sensor" and any(token in text for token in ("movimiento", "presencia", "radar", "pir", "ir", "puerta", "timbre", "camara", "seguridad")):
            roles.append(("security_sensor", "security", "actividad, movimiento y sensores de seguridad"))
        return roles

    def infer_entity_locations(self, row: sqlite3.Row | dict[str, Any], aliases: list[str]) -> list[tuple[str, str | None, str | None]]:
        entity_id = str(row["entity_id"] if isinstance(row, sqlite3.Row) else row.get("entity_id") or "")
        text = normalize_entity_alias(
            " ".join(
                str(part or "")
                for part in (
                    entity_id,
                    row["friendly_name"] if isinstance(row, sqlite3.Row) else row.get("friendly_name"),
                    row["area_name"] if isinstance(row, sqlite3.Row) else row.get("area_name"),
                    row["device_name"] if isinstance(row, sqlite3.Row) else row.get("device_name"),
                    " ".join(aliases),
                )
            )
        )
        known = {
            "sofa": ("salon", "casa"),
            "comedor": ("comedor", "casa"),
            "salon": ("salon", "casa"),
            "cocina": ("cocina", "casa"),
            "balconera": ("dormitorio pequeño", "casa"),
            "dormitorio pequeno": ("dormitorio pequeño", "casa"),
            "habitacion pequena": ("dormitorio pequeño", "casa"),
            "oficina": ("oficina", "casa"),
            "terreno": ("terreno", "exterior"),
            "huerto": ("huerto", "exterior"),
            "almacen": ("almacen", "exterior"),
            "bano": ("baño", "casa"),
            "vater": ("baño", "casa"),
            "wc": ("baño", "casa"),
        }
        locations: list[tuple[str, str | None, str | None]] = []
        for alias, (room, zone) in known.items():
            if alias in text:
                locations.append((alias, room, zone))
        return locations

    def sync_entity_semantics(
        self,
        row: sqlite3.Row | dict[str, Any],
        *,
        aliases: list[str],
        source: str,
    ) -> None:
        entity_id = str(row["entity_id"] if isinstance(row, sqlite3.Row) else row.get("entity_id") or "").strip()
        domain = str(row["domain"] if isinstance(row, sqlite3.Row) else row.get("domain") or entity_id.split(".", 1)[0])
        if not entity_id or not domain:
            return
        base_aliases = [
            str(row["friendly_name"] if isinstance(row, sqlite3.Row) else row.get("friendly_name") or ""),
            str(row["area_name"] if isinstance(row, sqlite3.Row) else row.get("area_name") or ""),
            str(row["device_name"] if isinstance(row, sqlite3.Row) else row.get("device_name") or ""),
            entity_id.replace(".", " "),
        ]
        for alias in [*base_aliases, *aliases]:
            if alias.strip():
                self.upsert_entity_alias(
                    entity_id=entity_id,
                    alias=alias,
                    domain=domain,
                    priority=80 if alias in aliases else 35,
                    confidence=0.9 if alias in aliases else 0.65,
                    source=source,
                )
        for role, criticality, intent_hint in self.infer_entity_roles(row, aliases):
            self.upsert_entity_role(
                entity_id=entity_id,
                role=role,
                criticality=criticality,
                intent_hint=intent_hint,
                source=source,
            )
        for location_alias, room, zone in self.infer_entity_locations(row, aliases):
            self.upsert_entity_location(
                entity_id=entity_id,
                location_alias=location_alias,
                room=room,
                zone=zone,
                source=source,
            )

    def migrate_entity_aliases_from_catalog(self) -> None:
        rows = list(
            self.conn.execute(
                """
                SELECT entity_id, domain, friendly_name, area_name, device_name, device_class, unit, aliases
                FROM entity_catalog
                """
            )
        )
        for row in rows:
            try:
                aliases = json.loads(row["aliases"] or "[]") if row["aliases"] else []
            except json.JSONDecodeError:
                aliases = []
            aliases = [str(alias).strip() for alias in aliases if str(alias).strip()]
            self.sync_entity_semantics(row, aliases=aliases, source="catalog_migration")

    def add_usage_event(
        self,
        *,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        estimated_cost_usd: float | None,
        context: str,
        provider: str | None,
        duration_ms: int | None,
        router_reason: str | None,
    ) -> None:
        if estimated_cost_usd is not None and estimated_cost_usd < 0:
            estimated_cost_usd = None
        self.conn.execute(
            """
            INSERT INTO usage_events(
                created_at, model, prompt_tokens, completion_tokens, total_tokens,
                estimated_cost_usd, context, provider, duration_ms, router_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                estimated_cost_usd,
                context[:120],
                provider,
                duration_ms,
                router_reason[:1000] if router_reason else None,
            ),
        )
        self.conn.commit()

    def usage_summary(self, limit: int = 10) -> dict[str, Any]:
        total = self.conn.execute(
            """
            SELECT
                COUNT(*) AS calls,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN estimated_cost_usd > 0 THEN estimated_cost_usd ELSE 0 END), 0) AS cost
            FROM usage_events
            """
        ).fetchone()
        recent = list(
            self.conn.execute(
                """
                SELECT created_at, model, prompt_tokens, completion_tokens, total_tokens,
                       estimated_cost_usd, context, provider, duration_ms, router_reason
                FROM usage_events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )
        return {"total": dict(total), "recent": [dict(row) for row in recent]}

    def estimate_usage_call(
        self,
        *,
        context: str,
        model: str,
        default_prompt_tokens: int,
        default_completion_tokens: int,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT AVG(prompt_tokens) AS prompt_tokens,
                   AVG(completion_tokens) AS completion_tokens,
                   AVG(estimated_cost_usd) AS cost_usd,
                   COUNT(*) AS samples
            FROM (
                SELECT prompt_tokens, completion_tokens, estimated_cost_usd
                FROM usage_events
                WHERE context = ? AND model = ?
                  AND prompt_tokens IS NOT NULL
                  AND completion_tokens IS NOT NULL
                ORDER BY id DESC
                LIMIT 40
            )
            """,
            (context, model),
        ).fetchone()
        samples = int(row["samples"] or 0) if row else 0
        return {
            "prompt_tokens": int(round(row["prompt_tokens"])) if samples else default_prompt_tokens,
            "completion_tokens": int(round(row["completion_tokens"])) if samples else default_completion_tokens,
            "cost_usd": float(row["cost_usd"]) if samples and row["cost_usd"] is not None else None,
            "samples": samples,
        }

    def recent_memories(self, limit: int = 12) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT id, created_at, kind, topic, content, confidence, source
                FROM memories
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def search_memories(self, query: str, limit: int = 10) -> list[sqlite3.Row]:
        terms = tokenize_search_query(query)
        if not terms:
            return self.recent_memories(limit)

        rows = list(
            self.conn.execute(
                """
                SELECT id, created_at, kind, topic, content, confidence, source
                FROM memories
                ORDER BY updated_at DESC, id DESC
                LIMIT 200
                """
            )
        )

        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            haystack = f"{row['topic']} {row['content']}".lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:limit]]

    def recent_observations(self, limit: int = 8) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT id, created_at, source, summary
                FROM observations
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def upsert_entity_catalog(self, rows: list[dict[str, Any]], *, source: str = "ha_search") -> None:
        if not rows:
            return
        now = utc_now()
        for row in rows:
            entity_id = str(row.get("entity_id") or "").strip()
            if not entity_id or "." not in entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            aliases = [str(alias).strip() for alias in row.get("aliases", []) if str(alias).strip()] if isinstance(row.get("aliases"), list) else []
            existing = self.conn.execute("SELECT aliases FROM entity_catalog WHERE entity_id = ?", (entity_id,)).fetchone()
            if existing:
                with contextlib.suppress(Exception):
                    aliases.extend(str(alias).strip() for alias in json.loads(existing["aliases"] or "[]") if str(alias).strip())
            aliases = list(dict.fromkeys(alias.casefold() for alias in aliases))
            aliases_text = json.dumps(aliases, ensure_ascii=False)
            semantic_row = {
                "entity_id": entity_id,
                "domain": domain,
                "friendly_name": row.get("friendly_name"),
                "area_name": row.get("area_name"),
                "device_name": row.get("device_name"),
                "device_class": row.get("device_class"),
                "unit": row.get("unit"),
            }
            self.conn.execute(
                """
                INSERT INTO entity_catalog(
                    entity_id, updated_at, domain, friendly_name, area_name, device_name,
                    device_class, unit, aliases, state, last_changed, last_updated, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    domain=excluded.domain,
                    friendly_name=excluded.friendly_name,
                    area_name=excluded.area_name,
                    device_name=excluded.device_name,
                    device_class=excluded.device_class,
                    unit=excluded.unit,
                    aliases=excluded.aliases,
                    state=excluded.state,
                    last_changed=excluded.last_changed,
                    last_updated=excluded.last_updated,
                    source=excluded.source
                """,
                (
                    entity_id,
                    now,
                    domain,
                    row.get("friendly_name"),
                    row.get("area_name"),
                    row.get("device_name"),
                    row.get("device_class"),
                    row.get("unit"),
                    aliases_text,
                    row.get("state"),
                    row.get("last_changed"),
                    row.get("last_updated"),
                    source[:80],
                ),
            )
            self.sync_entity_semantics(semantic_row, aliases=aliases, source=source[:80])
        self.conn.commit()

    def add_entity_resolution(
        self,
        *,
        query: str,
        domain: str,
        resolved_entity_id: str,
        candidates: list[dict[str, Any]],
        source: str = "resolver",
    ) -> None:
        query = query.strip()
        resolved_entity_id = resolved_entity_id.strip()
        if not query or not resolved_entity_id:
            return
        first = candidates[0] if candidates else {}
        self.conn.execute(
            """
            INSERT INTO entity_resolutions(
                created_at, query, domain, resolved_entity_id, friendly_name, score, candidates_json, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                query[:200],
                domain[:80],
                resolved_entity_id,
                first.get("friendly_name"),
                int(first.get("match_score") or 0),
                json.dumps(candidates[:8], ensure_ascii=False),
                source[:80],
            ),
        )
        self.conn.commit()

    def teach_entity_mapping(
        self,
        *,
        teaching_type: str,
        key_text: str,
        target_entity_id: str,
        source: str = "conversation",
        notes: str | None = None,
    ) -> None:
        teaching_type = teaching_type.strip().lower()
        if teaching_type not in {"alias", "replacement"}:
            raise ValueError("teaching_type debe ser alias o replacement")
        key_text = key_text.strip()
        normalized_key = (
            key_text.lower()
            if teaching_type == "replacement"
            else normalize_entity_alias(key_text)
        )
        target_entity_id = target_entity_id.strip().lower()
        if not normalized_key or "." not in target_entity_id:
            raise ValueError("Enseñanza de entidad incompleta")
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO entity_teachings(
                teaching_type, key_text, normalized_key, target_entity_id,
                source, notes, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(teaching_type, normalized_key) DO UPDATE SET
                key_text=excluded.key_text,
                target_entity_id=excluded.target_entity_id,
                source=excluded.source,
                notes=excluded.notes,
                active=1,
                updated_at=excluded.updated_at
            """,
            (
                teaching_type,
                key_text,
                normalized_key,
                target_entity_id,
                source[:80],
                notes,
                now,
                now,
            ),
        )
        self.conn.commit()

    def remove_entity_teaching(self, *, teaching_type: str, key_text: str) -> bool:
        teaching_type = teaching_type.strip().lower()
        normalized_key = (
            key_text.strip().lower()
            if teaching_type == "replacement"
            else normalize_entity_alias(key_text)
        )
        cursor = self.conn.execute(
            """
            UPDATE entity_teachings
            SET active = 0, updated_at = ?
            WHERE teaching_type = ? AND normalized_key = ? AND active = 1
            """,
            (utc_now(), teaching_type, normalized_key),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def list_entity_teachings(self, *, include_inactive: bool = False) -> list[sqlite3.Row]:
        where = "" if include_inactive else "WHERE active = 1"
        return list(
            self.conn.execute(
                f"""
                SELECT teaching_type, key_text, target_entity_id, source, notes,
                       active, created_at, updated_at
                FROM entity_teachings
                {where}
                ORDER BY active DESC, updated_at DESC, id DESC
                LIMIT 200
                """
            )
        )

    def resolve_taught_entity(self, reference: str, *, domain: str = "") -> str | None:
        reference = str(reference or "").strip()
        if not reference:
            return None
        requested_domains = {
            item.strip()
            for item in re.split(r"[, ]+", domain)
            if item.strip()
        }

        def domain_allowed(entity_id: str) -> bool:
            return not requested_domains or entity_id.split(".", 1)[0] in requested_domains

        def follow_replacements(entity_id: str) -> str:
            current = entity_id.lower()
            visited: set[str] = set()
            for _ in range(8):
                if current in visited:
                    break
                visited.add(current)
                row = self.conn.execute(
                    """
                    SELECT target_entity_id
                    FROM entity_teachings
                    WHERE teaching_type = 'replacement'
                      AND normalized_key = ? AND active = 1
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (current,),
                ).fetchone()
                if not row:
                    break
                current = str(row["target_entity_id"]).lower()
            return current

        current = follow_replacements(reference)
        if current != reference.lower() and domain_allowed(current):
            return current

        normalized_reference = normalize_entity_alias(reference)
        candidates: list[tuple[int, str]] = []
        for row in self.conn.execute(
            """
            SELECT normalized_key, target_entity_id
            FROM entity_teachings
            WHERE teaching_type = 'alias' AND active = 1
            """
        ):
            alias = str(row["normalized_key"])
            target = str(row["target_entity_id"]).lower()
            if not domain_allowed(target):
                continue
            if normalized_reference == alias or re.search(
                rf"(?<!\w){re.escape(alias)}(?!\w)", normalized_reference
            ):
                candidates.append((len(alias), target))
        if not candidates:
            return None
        target = max(candidates, key=lambda item: item[0])[1]
        return follow_replacements(target)

    def search_entity_catalog(self, query: str, domain: str = "", limit: int = 8) -> list[sqlite3.Row]:
        terms = tokenize_search_query(query)
        normalized_query = normalize_entity_alias(query)
        role_hints = query_role_hints(query)
        domains = [item.strip() for item in re.split(r"[, ]+", domain) if item.strip()]
        rows = list(
            self.conn.execute(
                """
                SELECT
                    c.entity_id, c.domain, c.friendly_name, c.area_name, c.device_name,
                    c.device_class, c.unit, c.aliases, c.state, c.updated_at,
                    GROUP_CONCAT(DISTINCT a.normalized_alias) AS normalized_aliases,
                    GROUP_CONCAT(DISTINCT a.alias) AS alias_rows,
                    GROUP_CONCAT(DISTINCT r.role) AS roles,
                    GROUP_CONCAT(DISTINCT l.normalized_location) AS normalized_locations
                FROM entity_catalog
                c
                LEFT JOIN entity_aliases a ON a.entity_id = c.entity_id
                LEFT JOIN entity_roles r ON r.entity_id = c.entity_id
                LEFT JOIN entity_locations l ON l.entity_id = c.entity_id
                GROUP BY c.entity_id
                ORDER BY c.updated_at DESC
                LIMIT 500
                """
            )
        )
        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            if domains and row["domain"] not in domains:
                continue
            alias_rows = str(row["alias_rows"] or "")
            normalized_aliases = str(row["normalized_aliases"] or "")
            roles = str(row["roles"] or "")
            normalized_locations = str(row["normalized_locations"] or "")
            haystack = fold_accents(
                " ".join(
                    str(part or "")
                    for part in (
                        row["entity_id"], row["friendly_name"], row["area_name"], row["device_name"],
                        row["device_class"], row["unit"], row["aliases"], row["state"],
                        alias_rows, normalized_aliases, roles, normalized_locations,
                    )
                )
            ).lower()
            score = sum(1 for term in terms if term in haystack) if terms else 1
            if normalized_query and normalized_query in normalized_aliases.split(","):
                score += 120
            elif normalized_query and normalized_query in normalized_aliases:
                score += 60
            if normalized_query and normalized_query in normalized_locations:
                score += 25
            if terms and all(term in normalized_aliases for term in terms):
                score += 35
            if terms and all(term in normalized_locations for term in terms):
                score += 25
            role_set = {role for role in roles.split(",") if role}
            if role_hints and role_set & role_hints:
                score += 90
            elif role_hints and role_set:
                score -= 80
            if "power_supply_status" in role_hints and row["entity_id"] in {"sensor.myups_status", "sensor.myups_status_data"}:
                score += 80
            if "power_supply_status" in role_hints and role_set & {"luminosity_sensor", "physical_light"}:
                score -= 70
            if "luminosity_sensor" in role_hints and role_set & {"physical_light", "power_supply_status"}:
                score -= 70
            if "physical_light" in role_hints and role_set & {"luminosity_sensor", "power_supply_status"}:
                score -= 100
            if "physical_light" in role_hints and row["domain"] in {"switch", "light"}:
                score += 35
            if "air_conditioner" in role_hints and row["entity_id"] in {
                "input_boolean.brokton_ac_dp1_switch",
                "input_boolean.brokton_ac_dp8_eco",
                "input_select.brokton_ac_dp4_mode",
            }:
                score += 90
            elif "air_conditioner" in role_hints and row["domain"] == "climate":
                score += 55
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:limit]]

    def recent_entity_resolutions(self, query: str = "", domain: str = "", limit: int = 8) -> list[sqlite3.Row]:
        terms = tokenize_search_query(query)
        domains = [item.strip() for item in re.split(r"[, ]+", domain) if item.strip()]
        rows = list(
            self.conn.execute(
                """
                SELECT created_at, query, domain, resolved_entity_id, friendly_name, score
                FROM entity_resolutions
                ORDER BY created_at DESC, id DESC
                LIMIT 200
                """
            )
        )
        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            if domains and row["domain"] not in domains:
                continue
            haystack = fold_accents(" ".join(str(row[key] or "") for key in row.keys())).lower()
            score = sum(1 for term in terms if term in haystack) if terms else 1
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:limit]]

    def add_task(
        self,
        *,
        run_at: str,
        title: str,
        instruction: str,
        interval_seconds: int | None = None,
        cancellation_key: str | None = None,
        priority: int = 50,
        max_attempts: int = DEFAULT_TASK_MAX_ATTEMPTS,
        retry_backoff_seconds: int = DEFAULT_TASK_RETRY_BACKOFF_SECONDS,
    ) -> int:
        now = utc_now()
        normalized_cancel = normalize_cancellation_key(cancellation_key or "") or None
        interval_seconds = validate_task_interval(interval_seconds)
        priority = max(0, min(int(priority), 100))
        max_attempts = max(1, min(int(max_attempts), 20))
        retry_backoff_seconds = max(1, min(int(retry_backoff_seconds), 86400))
        recovery_policy = task_recovery_policy(instruction)
        cursor = self.conn.execute(
            """
            INSERT INTO tasks(
                created_at, updated_at, run_at, status, title, instruction,
                priority, interval_seconds, cancellation_key, attempts, max_attempts,
                retry_backoff_seconds, recovery_policy
            )
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                now,
                now,
                parse_datetime_to_utc(run_at),
                title.strip()[:160],
                instruction.strip(),
                priority,
                interval_seconds,
                normalized_cancel,
                max_attempts,
                retry_backoff_seconds,
                recovery_policy,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_tasks(self, *, include_done: bool = False, limit: int = 30) -> list[sqlite3.Row]:
        statuses = ("pending", "running", "failed") if not include_done else (
            "pending",
            "running",
            "failed",
            "done",
            "cancelled",
        )
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.conn.execute(
                f"""
                SELECT id, created_at, updated_at, run_at, status, title, instruction,
                       result, last_error, priority, interval_seconds, cancellation_key,
                       attempts, max_attempts, retry_backoff_seconds, lease_owner,
                       lease_expires_at, last_started_at, recovery_policy
                FROM tasks
                WHERE status IN ({placeholders})
                ORDER BY run_at ASC, id ASC
                LIMIT ?
                """,
                (*statuses, limit),
            )
        )

    def get_due_tasks(self, *, limit: int = 5) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT id, run_at, status, title, instruction, priority, interval_seconds,
                       cancellation_key, attempts, max_attempts, retry_backoff_seconds,
                       recovery_policy
                FROM tasks
                WHERE status = 'pending' AND run_at <= ?
                ORDER BY priority DESC, run_at ASC, id ASC
                LIMIT ?
                """,
                (utc_now(), limit),
            )
        )

    def seconds_until_next_task(self, *, maximum: float = TASK_POLL_MAX_SECONDS) -> float:
        row = self.conn.execute(
            "SELECT run_at FROM tasks WHERE status = 'pending' ORDER BY run_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return maximum
        try:
            run_at = dt.datetime.fromisoformat(parse_datetime_to_utc(str(row["run_at"])))
            delay = (run_at - dt.datetime.now(dt.UTC)).total_seconds()
        except Exception:
            return 0.1
        return max(0.1, min(maximum, delay))

    def recover_expired_tasks(self) -> int:
        now = utc_now()
        rows = list(
            self.conn.execute(
                """
                SELECT id, run_at, interval_seconds, attempts, max_attempts,
                       retry_backoff_seconds, recovery_policy
                FROM tasks
                WHERE status = 'running'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now,),
            )
        )
        recovered = 0
        for row in rows:
            task_id = int(row["id"])
            interval = int(row["interval_seconds"] or 0)
            attempts = int(row["attempts"] or 0)
            max_attempts = max(1, int(row["max_attempts"] or DEFAULT_TASK_MAX_ATTEMPTS))
            policy = str(row["recovery_policy"] or "retry")
            message = "Ejecución interrumpida con resultado externo incierto."
            if policy == "manual" and interval <= 0:
                status = "failed"
                next_run = str(row["run_at"])
            elif policy == "manual" or attempts >= max_attempts:
                if interval > 0:
                    status = "pending"
                    next_run = next_interval_run_at(str(row["run_at"]), interval)
                    attempts = 0
                    message += " Se omite esta ocurrencia y se conserva la siguiente."
                else:
                    status = "failed"
                    next_run = str(row["run_at"])
            else:
                status = "pending"
                backoff = max(1, int(row["retry_backoff_seconds"] or DEFAULT_TASK_RETRY_BACKOFF_SECONDS))
                next_run = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=backoff)).isoformat(timespec="seconds")
                message += " Se reintentará de forma segura."
            cursor = self.conn.execute(
                """
                UPDATE tasks
                SET updated_at = ?, run_at = ?, status = ?, attempts = ?,
                    lease_owner = NULL, lease_expires_at = NULL, last_error = ?
                WHERE id = ? AND status = 'running'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now, next_run, status, attempts, message, task_id, now),
            )
            if cursor.rowcount:
                recovered += 1
                self.conn.execute(
                    """
                    UPDATE task_runs
                    SET finished_at = ?, status = 'interrupted', error = ?
                    WHERE task_id = ? AND status = 'running'
                    """,
                    (now, message, task_id),
                )
        self.conn.commit()
        return recovered

    def reset_running_tasks(self) -> None:
        self.recover_expired_tasks()

    def update_task(
        self,
        *,
        task_id: int,
        run_at: str | None = None,
        title: str | None = None,
        instruction: str | None = None,
        status: str | None = None,
    ) -> bool:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [utc_now()]
        if run_at is not None:
            fields.append("run_at = ?")
            values.append(parse_datetime_to_utc(run_at))
        if title is not None:
            fields.append("title = ?")
            values.append(title.strip()[:160])
        if instruction is not None:
            fields.extend(["instruction = ?", "recovery_policy = ?"])
            values.extend([instruction.strip(), task_recovery_policy(instruction)])
        if status is not None:
            fields.append("status = ?")
            values.append(status)
            if status != "running":
                fields.extend(["lease_owner = NULL", "lease_expires_at = NULL"])
            if status == "pending":
                fields.extend(["attempts = 0", "last_error = NULL"])
        values.append(task_id)
        cursor = self.conn.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def cancel_task(self, task_id: int) -> bool:
        now = utc_now()
        cursor = self.conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?, status = 'cancelled', lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = ? AND status IN ('pending', 'running', 'failed')
            """,
            (now, task_id),
        )
        if cursor.rowcount:
            self.conn.execute(
                """
                UPDATE task_runs
                SET finished_at = ?, status = 'cancelled', error = 'Cancelada durante la ejecución'
                WHERE task_id = ? AND status = 'running'
                """,
                (now, task_id),
            )
        self.conn.commit()
        return cursor.rowcount > 0

    def update_running_task_instruction(self, task_id: int, worker_id: str, instruction: str) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE tasks SET updated_at = ?, instruction = ?, recovery_policy = ?
            WHERE id = ? AND status = 'running' AND lease_owner = ?
            """,
            (utc_now(), instruction.strip(), task_recovery_policy(instruction), task_id, worker_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_task_running(self, task_id: int, worker_id: str) -> int | None:
        now = utc_now()
        lease_expires = (
            dt.datetime.now(dt.UTC) + dt.timedelta(seconds=TASK_TIMEOUT_SECONDS + TASK_LEASE_GRACE_SECONDS)
        ).isoformat(timespec="seconds")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cursor = self.conn.execute(
                """
                UPDATE tasks
                SET updated_at = ?, status = 'running', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, last_started_at = ?
                WHERE id = ? AND status = 'pending' AND run_at <= ?
                """,
                (now, worker_id, lease_expires, now, task_id, now),
            )
            if cursor.rowcount < 1:
                self.conn.rollback()
                return None
            row = self.conn.execute("SELECT attempts FROM tasks WHERE id = ?", (task_id,)).fetchone()
            attempt = int(row["attempts"] if row else 1)
            run_cursor = self.conn.execute(
                """
                INSERT INTO task_runs(task_id, worker_id, attempt, started_at, status)
                VALUES (?, ?, ?, ?, 'running')
                """,
                (task_id, worker_id, attempt, now),
            )
            self.conn.commit()
            return int(run_cursor.lastrowid)
        except Exception:
            self.conn.rollback()
            raise

    def _finish_run(self, run_id: int, status: str, *, result: str | None = None, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE task_runs
            SET finished_at = ?, status = ?, result = ?, error = ?
            WHERE id = ? AND status = 'running'
            """,
            (utc_now(), status, result[:4000] if result else None, error[:4000] if error else None, run_id),
        )

    def mark_task_done(self, task_id: int, worker_id: str, run_id: int, result: str) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE tasks SET updated_at = ?, status = 'done', result = ?, last_error = NULL,
                lease_owner = NULL, lease_expires_at = NULL
            WHERE id = ? AND status = 'running' AND lease_owner = ?
            """,
            (utc_now(), result.strip()[:4000], task_id, worker_id),
        )
        self._finish_run(run_id, "done" if cursor.rowcount else "superseded", result=result)
        self.conn.commit()
        return cursor.rowcount > 0

    def reschedule_after_success(
        self,
        task: sqlite3.Row,
        worker_id: str,
        run_id: int,
        result: str,
    ) -> str | None:
        interval = validate_task_interval(task["interval_seconds"])
        if interval is None:
            return None
        next_run = next_interval_run_at(str(task["run_at"]), interval)
        cursor = self.conn.execute(
            """
            UPDATE tasks SET updated_at = ?, run_at = ?, status = 'pending', result = ?,
                last_error = NULL, attempts = 0, lease_owner = NULL, lease_expires_at = NULL
            WHERE id = ? AND status = 'running' AND lease_owner = ?
            """,
            (utc_now(), next_run, result.strip()[:4000], int(task["id"]), worker_id),
        )
        self._finish_run(run_id, "done" if cursor.rowcount else "superseded", result=result)
        self.conn.commit()
        return next_run if cursor.rowcount else None

    def reschedule_running_task(
        self,
        *,
        task_id: int,
        worker_id: str,
        run_id: int,
        run_at: str,
        title: str | None = None,
        instruction: str | None = None,
    ) -> bool:
        fields = [
            "updated_at = ?",
            "run_at = ?",
            "status = 'pending'",
            "attempts = 0",
            "lease_owner = NULL",
            "lease_expires_at = NULL",
        ]
        values: list[Any] = [utc_now(), parse_datetime_to_utc(run_at)]
        if title is not None:
            fields.append("title = ?")
            values.append(title.strip()[:160])
        if instruction is not None:
            fields.extend(["instruction = ?", "recovery_policy = ?"])
            values.extend([instruction.strip(), task_recovery_policy(instruction)])
        values.extend([task_id, worker_id])
        cursor = self.conn.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ? AND status = 'running' AND lease_owner = ?",
            values,
        )
        self._finish_run(
            run_id,
            "rescheduled" if cursor.rowcount else "superseded",
            result=f"Reprogramada para {parse_datetime_to_utc(run_at)}",
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def fail_or_retry_task(
        self,
        task: sqlite3.Row,
        worker_id: str,
        run_id: int,
        error: str,
        *,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        task_id = int(task["id"])
        row = self.conn.execute(
            "SELECT attempts, max_attempts, retry_backoff_seconds FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        attempts = int(row["attempts"] if row else int(task["attempts"] or 0) + 1)
        max_attempts = max(1, int(row["max_attempts"] if row else task["max_attempts"] or DEFAULT_TASK_MAX_ATTEMPTS))
        backoff = max(1, int(row["retry_backoff_seconds"] if row else task["retry_backoff_seconds"] or DEFAULT_TASK_RETRY_BACKOFF_SECONDS))
        interval = int(task["interval_seconds"] or 0)
        recovery_policy = str(task["recovery_policy"] or "retry")
        if recovery_policy == "manual" and interval > 0:
            next_run = next_interval_run_at(str(task["run_at"]), interval)
            status = "pending"
            attempts = 0
            error = f"{error} No se reintenta esta ocurrencia porque sus efectos pueden haberse ejecutado; se conserva la siguiente."
        elif recovery_policy == "manual":
            next_run = str(task["run_at"])
            status = "failed"
            error = f"{error} No se reintenta automáticamente porque sus efectos pueden haberse ejecutado."
        elif attempts < max_attempts:
            delay = min(backoff * (2 ** max(0, attempts - 1)), 86400)
            next_run = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)).isoformat(timespec="seconds")
            status = "pending"
        elif interval > 0:
            next_run = next_interval_run_at(str(task["run_at"]), interval)
            status = "pending"
            attempts = 0
            error = f"{error} Se agotaron los reintentos de esta ocurrencia; se conserva la siguiente."
        else:
            next_run = str(task["run_at"])
            status = "failed"
        cursor = self.conn.execute(
            """
            UPDATE tasks SET updated_at = ?, run_at = ?, status = ?, attempts = ?,
                last_error = ?, lease_owner = NULL, lease_expires_at = NULL
            WHERE id = ? AND status = 'running' AND lease_owner = ?
            """,
            (utc_now(), next_run, status, attempts, error.strip()[:4000], task_id, worker_id),
        )
        run_status = "timeout" if timed_out else "failed"
        self._finish_run(run_id, run_status if cursor.rowcount else "superseded", error=error)
        self.conn.commit()
        return {
            "updated": cursor.rowcount > 0,
            "status": status,
            "next_run": next_run if status == "pending" else None,
            "attempts": attempts,
            "max_attempts": max_attempts,
        }


@dataclasses.dataclass
class RuntimeConfig:
    db_path: Path
    model_routes_path: Path
    agents_dir: Path
    site_profile_path: Path
    poll_seconds: int
    sensor_prompt: str
    enable_sensor_loop: bool
    require_action_confirmation: bool
    service_mode: bool
    mcp_url: str | None
    mcp_cmd: list[str]
    ha_base_url: str | None
    ha_token: str | None
    mcp_token: str | None
    fs_roots: list[Path]


class CodexonAgent:
    HA_MCP_ACTION_TOOL_NAMES = {
        "HassTurnOn",
        "HassTurnOff",
        "HassToggle",
        "HassCallService",
        "HassSetState",
        "HassSetPosition",
        "HassLightSet",
        "HassClimateSetTemperature",
        "HassClimateSetHvacMode",
        "HassMediaPlayerPlayMedia",
        "HassMediaPlayerPause",
        "HassMediaPlayerStop",
        "HassMediaPlayerVolumeSet",
        "HassVacuumStart",
        "HassVacuumReturnToBase",
        "HassLockLock",
        "HassLockUnlock",
        "HassCoverOpen",
        "HassCoverClose",
        "HassCoverStop",
    }

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        router: ModelRouter,
        session: ClientSession,
        memory: MemoryStore,
        tools: list[Any],
        require_action_confirmation: bool,
        ha_base_url: str | None,
        ha_token: str | None,
        fs_roots: list[Path],
        site_profile: SiteProfile | None = None,
    ) -> None:
        self.client = client
        self.router = router
        self.session = session
        self.memory = memory
        self.require_action_confirmation = require_action_confirmation
        self.ha_base_url = ha_base_url
        self.ha_token = ha_token
        self.fs_roots = fs_roots
        self.site_profile = site_profile or SiteProfile.empty()
        self.yaml = yaml
        self.httpx = httpx
        self.app_name = APP_NAME
        self.has_homeassistant_rest = bool(self.ha_base_url and self.ha_token)
        self.ask_lock = asyncio.Lock()
        self.preferred_model = self.memory.get_setting("interactive_model") or None
        self.model_test_question = self.memory.get_setting("model_test_question")
        self.request_preferred_model: str | None = None
        self.request_budget_mode: str | None = None
        self.last_interactive_model_used: str | None = None
        self.messages: list[dict[str, Any]] = []
        self.tool_name_map: dict[str, str] = {}
        self.worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.current_task_id: int | None = None
        self.current_task_run_id: int | None = None
        self.rescheduled_task_ids: set[int] = set()
        self.pending_critical_request: str | None = None
        self.pending_critical_category: str | None = None
        self.pending_lexical_correction: LexicalCorrection | None = None
        self.request_effective_user_text: str | None = None
        self.request_mobile_alert_sent = False
        self.current_task_mobile_alert_sent = False
        self.whatsapp_bridge: WhatsAppBridge | None = None
        self.openai_tools = self._convert_tools(tools)
        self.tool_names = [t.name for t in tools if not self.should_hide_mcp_tool(t.name)]
        self.tool_names.extend(
            [
                "schedule_task",
                "reschedule_current_task",
                "list_scheduled_tasks",
                "edit_scheduled_task",
                "cancel_scheduled_task",
                "wait_seconds",
                "whatsapp_send_message",
                "whatsapp_list_contacts",
                "whatsapp_get_status",
                "whatsapp_get_recent_messages",
            ]
        )
        self.tool_names.extend(builtin_tool_names(include_homeassistant=self.has_homeassistant_rest))

    def taught_entity(self, reference: str, *, domain: str = "") -> str | None:
        memory = getattr(self, "memory", None)
        resolver = getattr(memory, "resolve_taught_entity", None)
        return resolver(reference, domain=domain) if callable(resolver) else None

    def resolve_site_alias(self, text: str) -> tuple[str, str] | None:
        taught = self.taught_entity(text)
        if taught:
            return taught, text.strip()
        profile = getattr(self, "site_profile", None)
        resolved = profile.resolve_alias(text) if profile is not None else None
        if not resolved:
            return None
        replacement = self.taught_entity(resolved[0])
        return (replacement or resolved[0], resolved[1])

    def site_entity(self, role: str, default: str | None = None) -> str | None:
        profile = getattr(self, "site_profile", None)
        entity_id = profile.entity(role, default) if profile is not None else default
        return self.taught_entity(entity_id or "") or entity_id

    def site_entities(self, role: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        profile = getattr(self, "site_profile", None)
        entities = profile.entities(role, default) if profile is not None else default
        return tuple(
            self.taught_entity(entity_id) or entity_id
            for entity_id in entities
        )

    async def try_apply_explicit_entity_teaching(self, user_text: str) -> str | None:
        folded = normalize_entity_alias(user_text)
        teaching_verbs = (
            "cambia",
            "corrige",
            "altera",
            "sustituye",
            "reemplaza",
            "quita",
            "elimina",
            "borra",
            "pon",
            "asigna",
        )
        if not any(re.search(rf"\b{verb}\b", folded) for verb in teaching_verbs):
            return None

        entity_pattern = r"[a-z_]+\.[a-z0-9_]+"
        entity_ids = re.findall(rf"\b{entity_pattern}\b", folded)
        replacement_match = re.search(
            rf"\b(?:cambia|altera|sustituye|reemplaza)\s+"
            rf"(.+?)\s+(?:por|a)\s+({entity_pattern})\b",
            folded,
        )
        if replacement_match:
            old_reference = replacement_match.group(1).strip(" ,:;-")
            target_entity = replacement_match.group(2)
            args: dict[str, Any] = {
                "operation": "replace",
                "target_entity_id": target_entity,
                "notes": f"Enseñado explícitamente: {user_text[:300]}",
            }
            if "." in old_reference:
                args["old_entity_id"] = old_reference
            else:
                args["old_query"] = old_reference
            result = json.loads(
                await self.call_builtin_tool(
                    "__builtin__:ha_teach_entity_mapping", args
                )
            )
            return (
                "Corrección permanente guardada: "
                f"{result['old_entity_id']} → {result['target_entity_id']}."
            )

        if len(entity_ids) >= 2 and re.search(r"\b(?:por|a)\b", folded):
            result = json.loads(
                await self.call_builtin_tool(
                    "__builtin__:ha_teach_entity_mapping",
                    {
                        "operation": "replace",
                        "old_entity_id": entity_ids[0],
                        "target_entity_id": entity_ids[1],
                        "notes": f"Enseñado explícitamente: {user_text[:300]}",
                    },
                )
            )
            return (
                "Corrección permanente guardada: "
                f"{result['old_entity_id']} → {result['target_entity_id']}."
            )

        alias_match = re.search(
            rf"\b(?:corrige|pon|asigna)\s+(?:el\s+)?(?:alias\s+)?"
            rf"(.+?)\s+(?:por|como|a|es)\s+({entity_pattern})\b",
            folded,
        )
        if alias_match:
            alias = alias_match.group(1).strip(" ,:;-")
            target_entity = alias_match.group(2)
            result = json.loads(
                await self.call_builtin_tool(
                    "__builtin__:ha_teach_entity_mapping",
                    {
                        "operation": "alias",
                        "alias": alias,
                        "target_entity_id": target_entity,
                        "notes": f"Enseñado explícitamente: {user_text[:300]}",
                    },
                )
            )
            return (
                "Alias permanente guardado: "
                f"“{alias}” → {result['target_entity_id']}."
            )

        removal_match = re.search(
            r"\b(?:quita|elimina|borra)\s+(?:el\s+|la\s+)?"
            r"(alias|asociacion|sustitucion|reemplazo)\s+(.+?)\s*$",
            folded,
        )
        if removal_match:
            label = removal_match.group(2).strip(" ,:;-")
            teaching_type = (
                "replacement"
                if removal_match.group(1) in {"sustitucion", "reemplazo"}
                else "alias"
            )
            result = json.loads(
                await self.call_builtin_tool(
                    "__builtin__:ha_teach_entity_mapping",
                    {
                        "operation": "remove",
                        "teaching_type": teaching_type,
                        (
                            "old_entity_id"
                            if teaching_type == "replacement"
                            else "alias"
                        ): label,
                    },
                )
            )
            state = "eliminada" if result["removed"] else "no encontrada"
            return f"Enseñanza permanente {state}: {label}."
        return None

    def should_hide_mcp_tool(self, name: str) -> bool:
        return self.has_homeassistant_rest and name in self.HA_MCP_ACTION_TOOL_NAMES

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        used: set[str] = set()
        for tool in tools:
            if self.should_hide_mcp_tool(tool.name):
                continue
            name = safe_tool_name(tool.name)
            while name in used:
                name = safe_tool_name(f"{name}_x")
            used.add(name)
            self.tool_name_map[name] = tool.name
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.description or f"Herramienta MCP {tool.name}",
                        "parameters": getattr(
                            tool,
                            "inputSchema",
                            getattr(tool, "input_schema", {"type": "object", "properties": {}}),
                        )
                        or {"type": "object", "properties": {}, "additionalProperties": True},
                    },
                }
            )
        self.tool_name_map["schedule_automation"] = "__builtin__:schedule_automation"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "schedule_automation",
                    "description": (
                        "Crea una automatizacion futura persistente y estructurada. Usala para acciones de Home Assistant, "
                        "secuencias con esperas y condiciones sobre cualquier sensor o atributo. conditions admite sensores "
                        "binarios, numericos o de texto con operadores eq/ne/lt/lte/gt/gte/in/not_in. Para pulsos breves de "
                        "binary_sensor usa condition type=transition con from_state, to_state y after; no consultes solo el estado actual. steps admite llamadas "
                        "de servicio, esperas y acciones internas builtin como say_time, terminal_message, wait_seconds y ha_send_mobile_alert. "
                        "Usa este plan generico antes de crear nuevos formatos DETERMINISTIC_*. Si el usuario pide 'avisame cuando acabes', rellena completion_notification con enabled=true; "
                        "si rechaza el aviso, guarda completion_notification con enabled=false. "
                        "En sensores numericos, interpreta frases como 'cuando la humedad sea del 70', 'este en 70' o 'llegue a 70' "
                        "como umbral gte 70 salvo que el usuario diga explicitamente 'exactamente igual a 70'. "
                        "No escribas logica especifica por sensor."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "run_at": {
                                "type": "string",
                                "description": "Fecha/hora ISO 8601 de la primera evaluacion.",
                            },
                            "title": {"type": "string"},
                            "plan": automation_plan_json_schema(),
                            "interval_seconds": {"type": "integer", "minimum": 1},
                            "cancellation_key": {"type": "string"},
                            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
                        },
                        "required": ["run_at", "title", "plan"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["schedule_task"] = "__builtin__:schedule_task"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "schedule_task",
                    "description": (
                        "Crea una tarea futura persistente. Usala para agenda/programa/planifica/recuerdame/avisame, "
                        "dentro de/en N minutos-horas, a las/para las, mañana/esta noche, despues/luego/mas tarde, "
                        "cuando/en cuanto/si se cumple, cada/todos los y hasta que cancele/hasta nueva orden. "
                        "Para rangos de tiempo, crea una tarea de inicio y otra de fin. Para condiciones futuras tipo "
                        "'cuando X pase, haz Y', crea una tarea que evalúe la condición; si no se cumple debe llamar "
                        "reschedule_current_task con un delay, y si se cumple debe ejecutar la acción y terminar. "
                        "Para tareas repetitivas usa interval_seconds. La instrucción debe ser concreta y ejecutable."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "run_at": {
                                "type": "string",
                                "description": "Fecha/hora ISO 8601. Si no hay zona, se interpreta como Europe/Madrid.",
                            },
                            "title": {"type": "string", "description": "Título corto de la tarea."},
                            "instruction": {
                                "type": "string",
                                "description": "Qué debe ejecutar Codexon cuando llegue la hora.",
                            },
                            "interval_seconds": {
                                "type": "integer",
                                "description": "Intervalo de repetición en segundos. Usa 3600 para cada hora; omite si es una sola vez.",
                            },
                            "cancellation_key": {
                                "type": "string",
                                "description": "Frase clave opcional elegida por el usuario para cancelar esta tarea recurrente o pendiente. Ejemplo: 'para de hablar'.",
                            },
                            "max_attempts": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "default": DEFAULT_TASK_MAX_ATTEMPTS,
                                "description": "Intentos máximos por ejecución antes de dejarla fallida o saltar al siguiente intervalo.",
                            },
                            "retry_backoff_seconds": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 86400,
                                "default": DEFAULT_TASK_RETRY_BACKOFF_SECONDS,
                                "description": "Espera inicial entre reintentos; aumenta exponencialmente.",
                            },
                        },
                        "required": ["run_at", "title", "instruction"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["create_event_listener"] = "__builtin__:create_event_listener"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "create_event_listener",
                    "description": (
                        "Crea una escucha persistente del bus de eventos de Home Assistant. "
                        "Usala para peticiones reactivas como 'cuando cambie', 'en cuanto se active' "
                        "o 'cada vez que ocurra', en lugar de sondear con tareas periódicas. "
                        "Al coincidir el evento, Codexon encola una ejecución breve con instruction."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "instruction": {
                                "type": "string",
                                "description": "Acción concreta que se ejecutará cuando coincida el evento.",
                            },
                            "event_type": {
                                "type": "string",
                                "default": "state_changed",
                            },
                            "entity_id": {
                                "type": "string",
                                "description": "Obligatorio para state_changed.",
                            },
                            "from_state": {"type": "string"},
                            "to_state": {"type": "string"},
                            "attribute": {
                                "type": "string",
                                "description": "Ruta de atributo, por ejemplo current_temperature.",
                            },
                            "operator": {
                                "type": "string",
                                "enum": ["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in"],
                            },
                            "expected_value": {},
                            "event_data": {
                                "type": "object",
                                "description": "Campos que debe contener data para eventos personalizados.",
                            },
                            "cooldown_seconds": {
                                "type": "integer",
                                "minimum": 0,
                                "default": 0,
                            },
                            "once_only": {"type": "boolean", "default": False},
                            "priority": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 100,
                                "default": 50,
                            },
                            "cancellation_key": {"type": "string"},
                        },
                        "required": ["title", "instruction"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["list_event_listeners"] = "__builtin__:list_event_listeners"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "list_event_listeners",
                    "description": "Lista las escuchas persistentes de eventos de Home Assistant.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "include_disabled": {"type": "boolean", "default": False},
                            "limit": {"type": "integer", "default": 50},
                        },
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["cancel_event_listener"] = "__builtin__:cancel_event_listener"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "cancel_event_listener",
                    "description": "Desactiva una escucha persistente por id.",
                    "parameters": {
                        "type": "object",
                        "properties": {"listener_id": {"type": "integer"}},
                        "required": ["listener_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["reschedule_current_task"] = "__builtin__:reschedule_current_task"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "reschedule_current_task",
                    "description": (
                        "Reprograma la tarea programada que se está ejecutando ahora. Usala dentro de tareas condicionales "
                        "cuando la condición todavía no se cumpla. No crea tareas duplicadas."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "delay_minutes": {
                                "type": "integer",
                                "default": 5,
                                "description": "Minutos hasta la siguiente comprobación si no das run_at.",
                            },
                            "delay_seconds": {
                                "type": "number",
                                "description": "Segundos hasta la siguiente comprobación; alternativa más precisa a delay_minutes.",
                            },
                            "run_at": {"type": "string", "description": "Fecha/hora ISO 8601 opcional para la siguiente ejecución."},
                            "title": {"type": "string", "description": "Título opcional actualizado."},
                            "instruction": {"type": "string", "description": "Instrucción opcional actualizada."},
                            "reason": {"type": "string", "description": "Motivo breve de la reprogramación."},
                        },
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["list_scheduled_tasks"] = "__builtin__:list_scheduled_tasks"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "list_scheduled_tasks",
                    "description": "Lista tareas programadas en formato amigable para el usuario: id, estado, próxima ejecución local, frecuencia, clave de cancelación y resumen.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "include_done": {"type": "boolean", "default": False},
                            "limit": {"type": "integer", "default": 30},
                        },
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["edit_scheduled_task"] = "__builtin__:edit_scheduled_task"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "edit_scheduled_task",
                    "description": "Edita una tarea programada existente por id.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "integer"},
                            "run_at": {"type": "string"},
                            "title": {"type": "string"},
                            "instruction": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "cancelled"],
                                "description": "Usa pending para reactivar o cancelled para cancelar.",
                            },
                        },
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["cancel_scheduled_task"] = "__builtin__:cancel_scheduled_task"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "cancel_scheduled_task",
                    "description": "Cancela una tarea programada por id.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "integer"}},
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["wait_seconds"] = "__builtin__:wait_seconds"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "wait_seconds",
                    "description": "Espera una cantidad corta de segundos antes de continuar una secuencia de acciones.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {
                                "type": "number",
                                "description": "Segundos a esperar, entre 0.1 y 60.",
                            }
                        },
                        "required": ["seconds"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["whatsapp_send_message"] = "__builtin__:whatsapp_send_message"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "whatsapp_send_message",
                    "description": (
                        "Envía un mensaje por la sesión WhatsApp interna de Codexon. "
                        "Usa to='last_sender' para contestar o avisar al último chat recibido."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "to": {
                                "type": "string",
                                "default": "last_sender",
                            },
                        },
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["whatsapp_list_contacts"] = "__builtin__:whatsapp_list_contacts"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "whatsapp_list_contacts",
                    "description": (
                        "Lista los contactos y chats conocidos por la sesión WhatsApp. "
                        "Úsala antes de enviar si el usuario da un nombre ambiguo."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "default": ""},
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 200,
                                "default": 50,
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map["whatsapp_get_status"] = "__builtin__:whatsapp_get_status"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "whatsapp_get_status",
                    "description": (
                        "Consulta si WhatsApp está conectado, el contador de mensajes, "
                        "el último mensaje y cuántos contactos conoce."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        )
        self.tool_name_map[
            "whatsapp_get_recent_messages"
        ] = "__builtin__:whatsapp_get_recent_messages"
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": "whatsapp_get_recent_messages",
                    "description": (
                        "Lista mensajes recientes persistidos del canal WhatsApp, "
                        "incluyendo entrantes y salientes."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 20,
                            },
                            "direction": {
                                "type": "string",
                                "enum": ["all", "incoming", "outgoing"],
                                "default": "all",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            }
        )
        for schema in builtin_tool_schemas(include_homeassistant=self.has_homeassistant_rest):
            name = schema["function"]["name"]
            self.tool_name_map[name] = f"__builtin__:{name}"
            converted.append(schema)
        return converted

    def _system_prompt(self, user_text: str | None = None) -> str:
        economical = getattr(self, "request_budget_mode", None) == "economy"
        memories = self.memory.search_memories(user_text or "", limit=4 if economical else 10)
        entity_catalog = self.memory.search_entity_catalog(user_text or "", limit=5 if economical else 8)
        entity_resolutions = self.memory.recent_entity_resolutions(user_text or "", limit=4 if economical else 8)
        observations = self.memory.recent_observations(limit=3 if economical else 6)
        pending_tasks = self.memory.list_tasks(include_done=False, limit=4 if economical else 8)

        memory_block = "\n".join(
            f"- [{m['kind']}/{m['topic']}] {m['content']} (confianza {m['confidence']:.2f})"
            for m in memories
        ) or "- Sin memoria útil todavía."
        entity_catalog_block = "\n".join(
            f"- {e['entity_id']} = {e['friendly_name'] or 'sin nombre'}; area={e['area_name'] or '-'}; device={e['device_name'] or '-'}; class={e['device_class'] or '-'}; state={e['state']}"
            for e in entity_catalog
        ) or "- Sin catálogo relevante todavía."
        resolution_block = "\n".join(
            f"- '{r['query']}' ({r['domain'] or 'sin dominio'}) -> {r['resolved_entity_id']} ({r['friendly_name'] or 'sin nombre'}, score={r['score']})"
            for r in entity_resolutions
        ) or "- Sin resoluciones aprendidas todavía."
        observation_block = "\n".join(
            f"- {o['created_at']} {o['source']}: {o['summary']}" for o in observations
        ) or "- Sin observaciones recientes."
        task_block = "\n".join(
            f"- #{t['id']} {t['run_at']} [{t['status']}] {t['title']}: {t['instruction']}"
            for t in pending_tasks
        ) or "- Sin tareas pendientes."

        confirmation_rule = (
            "No ejecutes acciones que cambien el estado de Home Assistant, seguridad, alarmas, "
            "cerraduras, sirenas, cámaras o automatizaciones sin pedir confirmación explícita."
            if self.require_action_confirmation
            else "Puedes ejecutar acciones si el usuario las pide de forma clara."
        )

        schedule_hint = scheduling_intent_hint(user_text or "")
        site_context = self.site_profile.prompt_context()

        return f"""
Eres Codexon, un agente local de terminal para Home Assistant.
Objetivo: ayudar al usuario, observar sensores, detectar patrones útiles y recordar preferencias/hechos.
Fecha/hora local actual: {local_now().isoformat(timespec="seconds")}.
{schedule_hint}
Reglas:
- Usa herramientas para consultar Home Assistant. Para acciones que cambian estado en Home Assistant, usa las herramientas internas REST: primero ha_search_entities/ha_get_state para resolver entity_id y luego ha_call_service con confirm=true. No uses herramientas MCP HassTurnOn/HassTurnOff/HassToggle para acciones si ha_call_service está disponible.
- Si detectas una errata probable que cambie una acción física o una unidad crítica, pregunta antes de actuar con una frase tipo "¿Quieres decir ...?". No corrijas nombres de entidades, alias personales o ubicaciones si podrían ser vocabulario propio del usuario.
- Cuando el usuario use nombres naturales de sensores/dispositivos, no inventes entity_id. Usa ha_search_entities o ha_get_state con query/domain; fíjate en area_name/device_name para distinguir ubicaciones como Casa, Salón, Comedor, Oficina o Terreno. Conserva las ubicaciones que diga el usuario en query: para "interruptores en comedor o salon" usa query="comedor salon" y domain="switch,light", no query="interruptor". Si una herramienta indica ambigüedad, muestra candidatos y pide precisión antes de actuar.
- Para contar, listar o enumerar dispositivos por nombres naturales consulta primero ha_search_site_entities. Basa el número, nombres, roles y límites únicamente en su resultado e incluye siempre cada entity_id completo con dominio. Si semantic_name_known=false, di “sin zona o función asignada” y no inventes una descripción. Cantidad existente no significa máximo simultáneo: si simultaneous_limit_known=false, di únicamente que el límite simultáneo no está configurado; no afirmes que se puedan activar individualmente o en grupos ni propongas cifras, independencia, compatibilidad o recomendaciones. Una consulta de listado es de solo lectura: no crees tareas, escuchas ni automatizaciones.
- Para localizar la moto, mi moto, Sinotrak, el movil, mi movil, telefono o Samsung usa siempre traccar_get_location. Moto corresponde exclusivamente a Sinotrak y movil corresponde exclusivamente a Samsung. No uses otros device_tracker, Bluetooth, WiFi ni recuerdos antiguos. Indica siempre antiguedad y si la posicion no es actual.
- Ante una pregunta estadística, razona primero qué se quiere calcular: estado en un instante, transiciones/eventos, agregado de una serie numérica o consumo atribuible a los intervalos activos de un dispositivo. El nombre del dispositivo y la magnitud medida pueden pertenecer a entidades distintas.
- Resuelve las fuentes por significado: consulta primero roles, alias y zonas del perfil local, y después el catálogo real de Home Assistant si falta información. No supongas que cada válvula, máquina o zona tenga un sensor de consumo dedicado: un contador común puede medir varias ramas y la actividad de cada controlador permite atribuirle su parte.
- Para preguntas históricas usa ha_get_history o ha_get_logbook si están disponibles. Para valores alrededor de una hora local durante varios días usa ha_get_history_around_time. Para comparar o agrupar una serie numérica usa ha_aggregate_numeric_history. Si el recorder ya eliminó estados antiguos, usa ha_get_long_term_statistics con el mismo periodo y sensor.
- Cuando se pida cuánto consumió una válvula, zona, máquina o interruptor durante un periodo, resuelve una entidad de actividad y otra de medición y usa ha_measure_numeric_during_state. En riego, el controlador o switch delimita los intervalos y el role irrigation.flow_meter aporta el contador compartido. Esto es una relación semántica entre funciones, no una asociación fija entre entity_id.
- Interpreta periodos en la zona horaria local: `esta semana` o `semana actual` empieza el lunes a las 00:00 y termina ahora; `semana pasada` es la semana natural anterior; `ultimos 7 dias` es un intervalo movil. Nunca amplíes `ahora` al final futuro del día.
- Una respuesta estadística debe contestar la magnitud solicitada a partir del resultado de las herramientas: incluye total o valor, unidad, periodo y cualquier limitación de cobertura relevante. Los entity_id explican las fuentes pero nunca sustituyen el resultado. No afirmes que falta un sensor dedicado hasta haber consultado los roles y relaciones del perfil local.
- Para máximos, mínimos, medias, rangos u otras estadísticas necesitas un periodo explícito. Si el usuario no indica hoy, ayer, semana, mes, fechas u otro intervalo, pregunta qué periodo quiere analizar; no sustituyas la estadística por el estado actual ni elijas arbitrariamente todo el histórico disponible.
- No conviertas un número de día, como "día 10", en una hora.
- Desambiguacion estricta de "luz": "enciende/apaga/prende/pon la luz" es accion fisica sobre switch/light; "cuanta luz", "nivel de luz", "luminosidad", "lux" o "sensacion luminica" son sensores de iluminancia; "se ha ido la luz", "corte de luz", "sin luz", "hay corriente" o "suministro electrico" es ausencia/presencia de suministro; "precio de la luz", "luz barata/cara", "kWh" o "mejores horas para consumir" es PVPC/electricidad.
- Para acciones fisicas sobre luces busca en los dominios switch y light, ya que una instalacion puede usar reles como luminarias.
- Para climatizacion, energia, seguridad, riego y sensores ambientales usa primero los roles del perfil local. Si falta un role, descubre candidatos en Home Assistant y pide confirmacion antes de asociarlos.
- Cuando el usuario diga cambia, corrige, altera, sustituye, reemplaza, quita, elimina, pon o asigna una entidad/alias por otra, usa ha_teach_entity_mapping. Una correccion explicita como "X es Y, no Z" debe guardarse como alias de X hacia Y; un dispositivo reemplazado por otro debe guardarse con operation=replace. No te limites a guardar una memoria textual. Verifica el destino y comunica exactamente qué asociación permanente has cambiado.
- Para preguntas sobre precio de electricidad usa el role energy.price cuando exista; de lo contrario descubre un sensor monetario por kWh compatible.
- Para preguntas de cuántas veces se activó un sensor, cuántas activaciones hubo, cuántas aperturas hubo o cuántas transiciones off->on/on->off ocurrieron, usa siempre ha_count_state_transitions. No respondas estas preguntas solo con ha_get_state: el estado actual no cuenta activaciones históricas. Para sensores binarios de presencia/movimiento/IR, una activación normalmente es off->on.
- Para actividad de seguridad consulta todos los sensores relevantes de las areas solicitadas; no reduzcas una zona a un unico sensor salvo que el perfil lo indique.
- Para reacciones a cambios o eventos de Home Assistant usa create_event_listener: crea una escucha persistente y no sondea periódicamente. Usa schedule_automation para acciones a una hora futura, secuencias, esperas o evaluaciones que no puedan expresarse como evento. Usa schedule_task para recordatorios de texto libre. No prometas recordar algo sin crear una tarea o escucha persistente.
- El canal WhatsApp escucha permanentemente cuando está conectado: no hace falta crear una tarea de sondeo para recibir mensajes. Usa whatsapp_get_status para comprobarlo, whatsapp_list_contacts para listar o resolver usuarios, whatsapp_get_recent_messages para revisar mensajes y whatsapp_send_message para iniciar un envío. En mensajes entrantes de WhatsApp, responde normalmente: el puente enviará automáticamente la respuesta final al mismo chat y no debes llamar whatsapp_send_message salvo que el usuario pida enviar además a otro destinatario.
- No crees nuevos formatos DETERMINISTIC_* para ideas nuevas. Primero intenta expresarlas como AUTOMATION_PLAN_V1: conditions para sensores/umbrales/transiciones, steps service para Home Assistant, steps delay para esperas, steps builtin para acciones internas. Mantén DETERMINISTIC_* solo como compatibilidad temporal de casos antiguos complejos como riego por litros o AC con verificación específica.
- Para riego por cantidad no sustituyas litros por segundos. Usa los roles de valvula, contador, alarmas y, si existe, controlador de zona definidos en el perfil local.
- Si el usuario corrige una tarea pendiente reciente con frases como "no, te corrijo", "mejor que...", "cambialo", "modificalo" o "en vez de", edita la tarea pendiente más reciente con edit_scheduled_task. No crees una segunda tarea salvo que el usuario pida explicitamente otra tarea adicional.
- Para tareas repetitivas tipo "cada hora", "cada dia", "todos los dias", "hasta que yo cancele", "hasta que diga lo contrario" o "hasta nueva orden", crea una tarea con schedule_task e interval_seconds. No uses un número máximo de repeticiones: deja la tarea recurrente hasta que el usuario la cancele. Si el usuario da una frase concreta de parada/cancelación, por ejemplo "para si te envío la instrucción 'para de hablar'", guarda esa frase en cancellation_key.
- Para condiciones futuras tipo "cuando X pase, haz Y", "en cuanto X se active, haz Y", "cuando la temperatura sea mayor de 25 enciende Y" o "si X supera Y, avisa", no te limites a leer el valor actual ni crees sondeo periódico. Resuelve primero entity_id y crea create_event_listener con la transición u operador apropiado.
- En condiciones numericas de sensores, interpreta "cuando sea del 70", "cuando este en 70", "cuando llegue a 70" o "cuando alcance 70" como umbral mayor o igual: operador gte y valor 70. Usa eq solo si el usuario dice "exactamente igual", "igual exacto" o equivalente. Para humedad/temperatura/potencia, una igualdad exacta casi nunca es lo deseado.
- Para intervalos, crea dos tareas: una de inicio y otra de fin. Ejemplo: encender a las 23:00 y apagar a las 03:00 del día siguiente. Para esperas cortas dentro de una secuencia que se ejecuta ahora, usa wait_seconds y luego sigue con los pasos restantes.
- Excepcion para sirenas o avisadores pulsables: si el usuario pide activar una sirena durante N segundos/minutos, usa ha_press_entity_interval con confirm=true. Las sirenas catalogadas se tratan como pulsadores: una pulsacion/toggle inicia y otra pulsacion/toggle al final detiene. No hagas dos ha_call_service manuales ni dos acciones separadas para intervalos cortos de sirenas: la latencia del chat/Codex altera la duracion real. Si ha_press_entity_interval falla, informa del error y NO reintentes la misma sirena automaticamente; espera nueva confirmacion del usuario.
- Para listar/ver/consultar tareas programadas, pendientes, recurrentes o fallidas, usa list_scheduled_tasks. No reconstruyas el listado desde memoria ni devuelvas JSON crudo; la herramienta ya devuelve un formato amigable.
- Para editar o cancelar tareas usa edit_scheduled_task o cancel_scheduled_task.
- Para leer, contar, crear o borrar ficheros usa fs_list_dir, fs_read_file, fs_count_text, fs_write_file y fs_delete_path.
- Para sensores en ficheros usa sensor_read_file; para exportar datos usa data_write_file; para consultar páginas web públicas usa web_fetch_url.
- Si el usuario pide llamar, avisar o notificar, usa el destino configurado en el perfil o descubre los servicios notify disponibles. No afirmes que el usuario lo ha oido o leido; solo que la notificacion fue enviada.
- Las respuestas y resultados vuelven por el mismo canal de origen. En WhatsApp responde por WhatsApp; en el chat responde en el chat. No llames ha_send_mobile_alert por defecto, por criticidad, por completar una tarea ni por expresiones genericas como "avisame" o "confirmame".
- Solo usa ha_send_mobile_alert cuando el usuario pida explicitamente una notificacion, aviso o alerta al movil/telefono. La palabra movil usada para localizar un dispositivo no es una peticion de notificacion. Una tarea antigua sin preferencia explicita tampoco recibe aviso telefonico.
- Una tarea que ya esta en agenda nunca puede detenerse para preguntar ni esperar respuesta humana. Ejecuta la accion y devuelve el resultado por su canal normal; nunca preguntes si quiere una alerta movil.
- Toda frase donde el usuario pida que Codexon hable, diga, anuncie, avise por voz o reproduzca un mensaje hablado debe pasar por TTS en un media_player de Home Assistant. Interpreta peticiones naturales como "di hola por algun altavoz" como una peticion de voz. Primero usa ha_get_tts_media_players(query="", limit=100), que ya filtra destinos no audibles como mute o volumen 0. Si el usuario indica un destino claro, habla directamente sin pedir confirmación extra. Si no hay destino claro y hay varias opciones, ofrece una lista breve incluyendo nombres amistosos y entity_id. Para hablar usa ha_call_service con domain="tts", service="google_translate_say", service_data={{"cache": false, "language": "es", "entity_id": "media_player...", "message": "..."}}, sin confirmación extra. Tras una llamada TTS correcta, di que el mensaje fue enviado; no afirmes que se oyo o se reprodujo porque Home Assistant solo confirma la llamada al servicio.
- Configuracion especifica de esta instalacion:
{site_context}
- No borres ni sobrescribas ficheros salvo que el usuario lo pida de forma explícita.
- Solo puedes acceder a estas raíces de sistema de ficheros: {", ".join(str(root) for root in self.fs_roots)}.
- {confirmation_rule}
- Si un dato es incierto, dilo y consulta sensores si hace falta.
- Responde en español, directo y operativo.
- Cuando evalúes seguridad, incluye nivel de riesgo bajo/medio/alto y motivo.
- No inventes estados de sensores.

Memoria relevante:
{memory_block}

Catálogo de entidades relevante:
{entity_catalog_block}

Resoluciones aprendidas de búsquedas previas:
{resolution_block}

Observaciones recientes:
{observation_block}

Tareas pendientes:
{task_block}
""".strip()

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: bool = True,
        task: str = "chat",
        priority: str = "cost",
        max_budget_usd: float | None = None,
        preferred_model: str | None = None,
        requires_memory: bool = False,
        ignore_request_preference: bool = False,
    ) -> Any:
        route = (self.router.config.get("routes") or {}).get(task) or {}
        selected_setting = MODEL_PREFERENCE_SETTING_BY_TASK.get(
            task, "interactive_model"
        )
        get_setting = getattr(self.memory, "get_setting", None)
        persisted_preference = (
            get_setting(selected_setting) or None
            if callable(get_setting)
            else None
        ) if not ignore_request_preference else None
        economy_preference = (
            ECONOMY_MODEL
            if getattr(self, "request_budget_mode", None) == "economy"
            else None
        )
        effective_preferred_model = economy_preference or preferred_model or (
            persisted_preference
            or (
                self.request_preferred_model
                if selected_setting == "interactive_model"
                else None
            )
            if not ignore_request_preference
            else None
        )
        effective_priority = str(route.get("priority") or priority)
        selection = self.router.select(
            ModelRequest(
                task=task,
                prompt_tokens_estimate=estimate_prompt_tokens(messages),
                requires_tools=tools and bool(self.openai_tools),
                requires_memory=requires_memory,
                priority=effective_priority,
                max_budget_usd=max_budget_usd,
                preferred_model=effective_preferred_model,
            )
        )
        kwargs: dict[str, Any] = {
            "model": selection.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools and self.openai_tools:
            kwargs["tools"] = self.openai_tools
            kwargs["tool_choice"] = "auto"

        last_exc: Exception | None = None
        attempted = [selection.model, *selection.fallbacks]
        for index, model in enumerate(attempted):
            kwargs["model"] = model
            started = time.perf_counter()
            try:
                response = await self.client.chat.completions.create(**kwargs)
                if not getattr(response, "choices", None):
                    raise RuntimeError(f"Respuesta LLM sin choices para {model}")
                duration_ms = int((time.perf_counter() - started) * 1000)
                reason = selection.reason if index == 0 else f"fallback tras fallo: {last_exc}"
                self.record_usage(
                    response,
                    model=model,
                    task=task,
                    reason=reason,
                    estimated_pre_cost_usd=selection.estimated_cost_usd if index == 0 else None,
                    duration_ms=duration_ms,
                )
                return response
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.memory.add_event(
                    level="warn",
                    message=f"Fallo modelo {model} para tarea {task}: {exception_summary(exc)}",
                )
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("ModelRouter no devolvió modelos candidatos")

    def selected_model_for_task(self, task: str, *, economy: bool = False) -> str:
        if economy:
            return ECONOMY_MODEL
        setting = MODEL_PREFERENCE_SETTING_BY_TASK.get(task, "interactive_model")
        selected = self.memory.get_setting(setting) or None
        route = (self.router.config.get("routes") or {}).get(task) or {}
        return str(selected or route.get("model") or self.router.config.get("default") or FALLBACK_MODEL)

    def estimate_request_budget(self, user_text: str, *, economy: bool = False) -> dict[str, Any]:
        labels = {
            "classification": "Clasificar la peticion",
            "statistical_planning": "Preparar plan estadistico (solo si aplica)",
            "homeassistant": "Resolver herramientas y redactar/verificar",
            "memory_extraction": "Guardar memoria relevante",
        }
        stages: list[dict[str, Any]] = []
        total_usd = 0.0
        unknown_cost = False
        for task, (default_input, default_output, calls) in BUDGET_STAGE_DEFAULTS.items():
            if economy and task == "homeassistant":
                default_input = 12000
                default_output = 450
            model = self.selected_model_for_task(task, economy=economy)
            usage = self.memory.estimate_usage_call(
                context=task,
                model=model,
                default_prompt_tokens=default_input,
                default_completion_tokens=default_output,
            )
            per_call = usage["cost_usd"]
            if per_call is None:
                per_call = self.router.estimate_cost(
                    model,
                    int(usage["prompt_tokens"]),
                    int(usage["completion_tokens"]),
                )
            stage_cost = per_call * calls if per_call is not None else None
            if stage_cost is None:
                unknown_cost = True
            else:
                total_usd += stage_cost
            stages.append(
                {
                    "task": task,
                    "label": labels[task],
                    "model": model,
                    "calls": calls,
                    "estimated_cost_usd": stage_cost,
                    "historical_samples": usage["samples"],
                    "conditional": task == "statistical_planning",
                }
            )
        return {
            "request": user_text,
            "mode": "economy" if economy else "normal",
            "stages": stages,
            "estimated_cost_usd": None if unknown_cost else total_usd,
            "estimated_cost_cents_usd": None if unknown_cost else total_usd * 100,
            "threshold_cents_usd": 0.2,
            "within_threshold": None if unknown_cost else total_usd * 100 < 0.2,
            "estimation": "recent_usage_average_then_catalog_price",
        }

    def total_usage_cost_usd(self) -> float:
        return float(self.memory.usage_summary(limit=0)["total"]["cost"] or 0.0)

    async def semantic_request_kind(self, user_text: str) -> str:
        response = await self._chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Clasifica la peticion sin resolverla. Devuelve solo JSON: "
                        "{\"kind\":\"statistical\"} si exige calcular maximo, minimo, "
                        "media, mediana, desviacion, tendencia, anomalia, suma, recuento "
                        "o comparacion historica; en cualquier otro caso devuelve "
                        "{\"kind\":\"other\"}. Una condicion futura con umbral no es "
                        "estadistica."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            tools=False,
            task="classification",
        )
        with contextlib.suppress(Exception):
            return str(
                extract_json_object(response.choices[0].message.content or "").get("kind")
                or "other"
            ).strip().lower()
        return "other"

    async def semantic_statistical_plan(
        self, user_text: str
    ) -> SemanticStatisticalPlan | None:
        if await self.semantic_request_kind(user_text) != "statistical":
            return None
        now = local_now().isoformat(timespec="seconds")
        response = await self._chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Clasifica semanticamente la peticion, sin depender de palabras exactas. "
                        "Una consulta estadistica pide calcular maximo, minimo, media, suma, rango, "
                        "mediana, desviacion, tendencia, anomalia, recuento o comparacion sobre "
                        "estados/eventos de Home Assistant. Una lectura "
                        "actual, una accion, una lista de entidades o una pregunta general es other. "
                        "Separa la magnitud medida del dispositivo cuya actividad puede atribuirle el "
                        "consumo. No inventes entity_id. Devuelve exclusivamente JSON con: "
                        "kind (statistical|other), metric, aggregation "
                        "(maximum|minimum|average|median|standard_deviation|trend|anomaly|sum|range|count|comparison), scope, "
                        "semantic_query en español con magnitud y zona pero sin verbos estadisticos, "
                        "period_status (explicit|missing), period_text, period_kind "
                        "(missing|today|yesterday|current_week|previous_week|current_month|"
                        "previous_month|rolling_days|explicit_dates|all_history), start_time, "
                        "end_time, rolling_days, clarification_question, "
                        "needs_activity_attribution (boolean), activity_scope. "
                        f"Fecha local actual: {now}. Considera periodo explicit solo si el usuario "
                        "indica hoy, ayer, semana, mes, fechas, desde/hasta, ultimos N o todo el historico. "
                        "Si falta, escribe clarification_question en español natural y no inventes periodo."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            tools=False,
            task="statistical_planning",
        )
        return parse_semantic_statistical_plan(
            response.choices[0].message.content or ""
        )

    def sensor_architecture_context(
        self, plan: SemanticStatisticalPlan
    ) -> dict[str, Any]:
        query = plan.semantic_query or " ".join(
            item for item in (plan.metric, plan.scope) if item
        )
        profile_matches: list[tuple[str, dict[str, Any]]] = []
        seen_roles: set[str] = set()
        for candidate_query in (query, plan.scope, plan.metric, plan.activity_scope):
            if not candidate_query:
                continue
            with contextlib.suppress(Exception):
                for role, binding in self.site_profile.search_bindings(candidate_query):
                    if role not in seen_roles:
                        profile_matches.append((role, binding))
                        seen_roles.add(role)

        catalog_rows: list[Any] = []
        catalog_entities: set[str] = set()
        for candidate_query in (query, plan.metric, plan.scope, plan.activity_scope):
            if not candidate_query:
                continue
            with contextlib.suppress(Exception):
                for row in self.memory.search_entity_catalog(
                    candidate_query, domain="sensor", limit=16
                ):
                    entity_id = str(
                        row.get("entity_id") if isinstance(row, dict) else row["entity_id"]
                    )
                    if entity_id and entity_id not in catalog_entities:
                        catalog_rows.append(row)
                        catalog_entities.add(entity_id)

        def row_value(row: Any, key: str) -> Any:
            if isinstance(row, dict):
                return row.get(key)
            with contextlib.suppress(Exception):
                return row[key]
            return None

        catalog_by_entity = {
            str(row_value(row, "entity_id") or ""): row
            for row in catalog_rows
            if row_value(row, "entity_id")
        }
        nodes: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        relationships: list[dict[str, Any]] = []
        functional_groups: dict[str, list[str]] = {}
        for role, binding in profile_matches:
            group = role.split(".", 1)[0]
            functional_groups.setdefault(group, []).append(role)
            for entity_id in self.site_profile.entities(role):
                row = catalog_by_entity.get(entity_id)
                if entity_id not in node_ids:
                    domain = entity_id.split(".", 1)[0]
                    nodes.append(
                        {
                            "entity_id": entity_id,
                            "domain": domain,
                            "capability": (
                                "measurement" if domain == "sensor" else
                                "binary_observation" if domain == "binary_sensor" else
                                "activity_or_control"
                            ),
                            "role": role,
                            "label": binding.get("label"),
                            "kind": binding.get("kind"),
                            "area": binding.get("area"),
                            "aliases": list(binding.get("aliases") or []),
                            "tags": list(binding.get("tags") or []),
                            "device_class": row_value(row, "device_class"),
                            "unit": row_value(row, "unit"),
                            "friendly_name": row_value(row, "friendly_name"),
                        }
                    )
                    node_ids.add(entity_id)
                master_role = str(binding.get("master_role") or "").strip()
                if master_role:
                    relationships.append(
                        {"type": "controlled_by_role", "from_role": role, "to_role": master_role}
                    )
                controller = binding.get("controller")
                if controller:
                    relationships.append(
                        {"type": "controller_metadata", "role": role, "value": controller}
                    )

        for row in catalog_rows:
            entity_id = str(row_value(row, "entity_id") or "")
            if not entity_id or entity_id in node_ids:
                continue
            nodes.append(
                {
                    "entity_id": entity_id,
                    "domain": str(row_value(row, "domain") or "sensor"),
                    "capability": "measurement",
                    "role": str(row_value(row, "roles") or ""),
                    "label": row_value(row, "friendly_name"),
                    "area": row_value(row, "area_name"),
                    "device_name": row_value(row, "device_name"),
                    "device_class": row_value(row, "device_class"),
                    "unit": row_value(row, "unit"),
                    "aliases": row_value(row, "aliases"),
                    "source": "learned_entity_catalog",
                }
            )
            node_ids.add(entity_id)

        for group, roles in functional_groups.items():
            measurement_roles = [
                role
                for role in roles
                if any(
                    node.get("role") == role
                    and node.get("capability") == "measurement"
                    for node in nodes
                )
            ]
            activity_roles = [
                role
                for role in roles
                if any(
                    node.get("role") == role
                    and node.get("capability") == "activity_or_control"
                    for node in nodes
                )
            ]
            if measurement_roles and activity_roles:
                relationships.append(
                    {
                        "type": "shared_functional_system",
                        "group": group,
                        "measurement_roles": measurement_roles,
                        "activity_roles": activity_roles,
                    }
                )

        return {
            "query": query,
            "requested_metric": plan.metric,
            "requested_scope": plan.scope,
            "needs_activity_attribution": plan.needs_activity_attribution,
            "activity_scope": plan.activity_scope,
            "nodes": nodes[:40],
            "relationships": relationships[:40],
            "functional_groups": functional_groups,
            "reasoning_rules": [
                "measurement nodes provide numeric evidence",
                "activity_or_control nodes delimit operation but do not measure by themselves",
                "shared functional groups may connect a controller with a common meter",
                "prefer explicit profile roles and learned replacements over name similarity",
                "compare every applicable measurement node before selecting an extreme",
            ],
        }

    def record_usage(
        self,
        response: Any,
        *,
        model: str,
        task: str,
        reason: str,
        estimated_pre_cost_usd: float | None,
        duration_ms: int,
    ) -> None:
        if task in {"homeassistant", "chat"}:
            self.last_interactive_model_used = model
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        estimated_cost = estimated_pre_cost_usd
        prices = self.router.price_tuple(model)
        if prices and prompt_tokens is not None and completion_tokens is not None:
            input_price, output_price = prices
            estimated_cost = (prompt_tokens * input_price) + (completion_tokens * output_price)
        provider = None
        extra = getattr(response, "model_extra", None)
        if isinstance(extra, dict):
            provider = extra.get("provider") or extra.get("provider_name")
        self.memory.add_usage_event(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            context=task,
            provider=provider,
            duration_ms=duration_ms,
            router_reason=reason,
        )
        runtime_log(
            "info",
            "llm",
            "llm_call",
            task=task,
            model=model,
            provider=provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            duration_ms=duration_ms,
            reason=reason,
        )

    async def ask(
        self,
        user_text: str,
        task: str = "homeassistant",
        preferred_model: str | None = None,
        budget_mode: str | None = None,
    ) -> str:
        async with self.ask_lock:
            previous_preferred = self.request_preferred_model
            previous_budget_mode = self.request_budget_mode
            self.request_preferred_model = preferred_model
            self.request_budget_mode = budget_mode
            self.last_interactive_model_used = None
            self.request_effective_user_text = user_text
            self.request_mobile_alert_sent = False
            try:
                answer = await self._ask_unlocked(user_text, task=task)
                effective_text = self.request_effective_user_text or user_text
                if (
                    task == "homeassistant"
                    and mobile_notification_explicitly_requested(effective_text)
                    and not scheduling_intent_hint(effective_text)
                    and not self.request_mobile_alert_sent
                ):
                    try:
                        await self.call_builtin_tool(
                            "__builtin__:ha_send_mobile_alert",
                            {
                                "title": "Codexon",
                                "message": f"He terminado. {answer[:500]}",
                            },
                        )
                    except Exception as exc:  # La operacion ya termino; no debe repetirse por fallar el aviso.
                        self.memory.add_event(
                            level="error",
                            message=f"Fallo enviando aviso de finalizacion interactivo: {exc}",
                        )
                        answer += f"\nNo pude enviar el aviso móvil de finalización: {exc}"
                return answer
            finally:
                self.request_preferred_model = previous_preferred
                self.request_budget_mode = previous_budget_mode

    async def _ask_unlocked(self, user_text: str, *, task: str) -> str:
        conversation_user_text = user_text
        statistical_plan: SemanticStatisticalPlan | None = None
        pending_lexical = getattr(self, "pending_lexical_correction", None)
        if task == "homeassistant" and pending_lexical:
            preference = notification_preference_reply(user_text)
            if preference is True:
                self.pending_lexical_correction = None
                user_text = pending_lexical.corrected
                conversation_user_text = user_text
            elif preference is False:
                self.pending_lexical_correction = None
                answer = "Vale, no aplico esa corrección ni ejecuto la orden."
                self.messages.extend(
                    [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(user_text, answer)
                return answer
            else:
                self.pending_lexical_correction = None

        pending_request = getattr(self, "pending_critical_request", None)
        if task == "homeassistant" and pending_request:
            preference = notification_preference_reply(user_text)
            if preference is not None:
                self.pending_critical_request = None
                self.pending_critical_category = None
                preference_text = (
                    "El usuario confirma: avisame al movil cuando termines y confirma el resultado."
                    if preference
                    else "El usuario confirma que se ejecute sin alerta movil de finalizacion."
                )
                user_text = f"{pending_request}\n{preference_text}"
            elif critical_action_requested(user_text):
                self.pending_critical_request = None
                self.pending_critical_category = None

        self.request_effective_user_text = user_text
        if task == "homeassistant":
            try:
                teaching_answer = await self.try_apply_explicit_entity_teaching(
                    user_text
                )
            except (RuntimeError, ValueError) as exc:
                teaching_answer = (
                    "No he guardado la corrección porque no pude verificarla sin "
                    f"ambigüedad: {exc}"
                )
            if teaching_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": teaching_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, teaching_answer)
                return teaching_answer

            try:
                statistical_plan = await self.semantic_statistical_plan(user_text)
            except Exception as exc:  # La clasificacion semantica no debe romper otras peticiones.
                self.memory.add_event(
                    level="warn",
                    message=(
                        "No se pudo crear el plan estadistico semantico: "
                        f"{exception_summary(exc)}"
                    ),
                )
            if statistical_plan and statistical_plan.period_status == "missing":
                answer = statistical_plan.clarification_question or (
                    "¿De qué periodo quieres que calcule esa estadística: hoy, ayer, "
                    "esta semana, este mes o unas fechas concretas?"
                )
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            whatsapp_water_answer = self.try_schedule_whatsapp_water_liters(user_text)
            if whatsapp_water_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": whatsapp_water_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, whatsapp_water_answer)
                return whatsapp_water_answer

            lexical = lexical_correction_suggestion(user_text)
            if lexical and lexical.requires_confirmation:
                self.pending_lexical_correction = lexical
                answer = (
                    f"¿Quieres decir: \"{lexical.corrected}\"?\n"
                    f"Motivo: {lexical.reason}\n"
                    "Responde sí para continuar con esa corrección o no para cancelarlo."
                )
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            tracker = requested_tracker(user_text)
            if tracker:
                try:
                    location_data = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:traccar_get_location",
                            {"device": tracker},
                        )
                    )
                    answer = str(location_data.get("recommended_answer") or "").strip()
                    if not answer:
                        raise ValueError("Traccar no devolvio una respuesta de ubicacion")
                except Exception as exc:  # No sustituir un fallo real por otro tracker.
                    answer = f"No pude consultar la ubicacion en Traccar: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                return answer

            correction_answer = self.try_correct_recent_scheduled_task(user_text)
            if correction_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": correction_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, correction_answer)
                return correction_answer

            listener_cancel_answer = self.try_cancel_event_listener_semantic(user_text)
            if listener_cancel_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": listener_cancel_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, listener_cancel_answer)
                return listener_cancel_answer

            event_listener_answer = self.try_create_simple_event_listener(user_text)
            if event_listener_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": event_listener_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, event_listener_answer)
                return event_listener_answer

            security_activity = requested_security_activity(user_text)
            if security_activity:
                try:
                    activity_data = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_security_activity",
                            security_activity,
                        )
                    )
                    answer = str(activity_data.get("recommended_answer") or "").strip()
                    if not answer:
                        raise ValueError("La consulta de actividad de seguridad no devolvio resumen")
                except Exception as exc:
                    answer = f"No pude consultar la actividad de seguridad: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            if requested_light_sensation(user_text):
                try:
                    state_rows: list[tuple[str, dict[str, Any]]] = []
                    for entity_id in ("sensor.muralcocina_tsl2561_sensor_luz", "sensor.itorre692_solar_radiation"):
                        state_rows.append(
                            (
                                entity_id,
                                json.loads(
                                    await self.call_builtin_tool(
                                        "__builtin__:ha_get_state",
                                        {"entity_id": entity_id},
                                    )
                                ),
                            )
                        )
                    answer = format_light_sensation_answer(state_rows)
                except Exception as exc:
                    answer = f"No pude consultar la sensación lumínica: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            if requested_power_supply_status(user_text):
                try:
                    state_rows = []
                    for entity_id in (
                        "sensor.myups_status",
                        "sensor.myups_status_data",
                        "sensor.myups_battery_charge",
                        "sensor.myups_load",
                        "sensor.myups_input_voltage",
                        "sensor.myups_output_voltage",
                        "sensor.powcasa_enchufes_powcasa_enchufes_voltage",
                        "sensor.powcasa_luces_persianas_powcasa_luces_persianas_voltage",
                        "sensor.piaoficina_voltage",
                        "sensor.pia_casetaherramientas_voltage",
                        "sensor.powcasa_enchufes_powcasa_enchufes_power",
                        "sensor.powcasa_luces_persianas_powcasa_luces_persianas_power",
                    ):
                        state_rows.append(
                            (
                                entity_id,
                                json.loads(
                                    await self.call_builtin_tool(
                                        "__builtin__:ha_get_state",
                                        {"entity_id": entity_id},
                                    )
                                ),
                            )
                        )
                    answer = format_power_supply_status_answer(state_rows)
                except Exception as exc:
                    answer = f"No pude comprobar el suministro eléctrico: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            if requested_ac_until_price_drop_plan(user_text):
                try:
                    pvpc = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_pvpc_cheapest_hours",
                            {"sensor_entity_id": "sensor.pvpc_dh", "limit": 24, "include_all_hours": True},
                        )
                    )
                    ac_state = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_state",
                            {"entity_id": "input_boolean.brokton_ac_dp1_switch"},
                        )
                    )
                    power_state = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_state",
                            {"entity_id": "sensor.powcasa_enchufes_powcasa_enchufes_power"},
                        )
                    )
                    answer = format_ac_until_price_drop_plan(
                        pvpc=pvpc,
                        ac_state=ac_state,
                        power_state=power_state,
                    )
                except Exception as exc:
                    answer = f"No pude calcular el control del aire hasta bajada de precio kWh: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            if requested_ac_pvpc_valley_plan(user_text):
                try:
                    pvpc = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_pvpc_cheapest_hours",
                            {"sensor_entity_id": "sensor.pvpc_dh", "limit": 24, "include_all_hours": True},
                        )
                    )
                    ac_state = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_state",
                            {"entity_id": "input_boolean.brokton_ac_dp1_switch"},
                        )
                    )
                    power_state = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_state",
                            {"entity_id": "sensor.powcasa_enchufes_powcasa_enchufes_power"},
                        )
                    )
                    answer = format_ac_pvpc_valley_plan(
                        pvpc=pvpc,
                        ac_state=ac_state,
                        power_state=power_state,
                    )
                except Exception as exc:
                    answer = f"No pude calcular el plan de encendido/apagado del aire por valle PVPC: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            if requested_ac_pvpc_shutdown_proposal(user_text):
                try:
                    pvpc = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_pvpc_cheapest_hours",
                            {"sensor_entity_id": "sensor.pvpc_dh", "limit": 24, "include_all_hours": True},
                        )
                    )
                    ac_state = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_state",
                            {"entity_id": "input_boolean.brokton_ac_dp1_switch"},
                        )
                    )
                    power_state = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_state",
                            {"entity_id": "sensor.powcasa_enchufes_powcasa_enchufes_power"},
                        )
                    )
                    answer = format_ac_pvpc_shutdown_proposal(
                        pvpc=pvpc,
                        ac_state=ac_state,
                        power_state=power_state,
                    )
                except Exception as exc:
                    answer = f"No pude calcular la recomendación de apagado del aire por precio kWh: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            climate_strategy = requested_climate_strategy(user_text)
            climate_temperature_entities = self.site_entities("environment.indoor_temperature")
            climate_control_entity = self.site_entity("climate.primary.control")
            if climate_strategy and climate_temperature_entities and climate_control_entity:
                try:
                    temp_rows: list[tuple[str, dict[str, Any]]] = []
                    for entity_id in climate_temperature_entities:
                        temp_rows.append(
                            (
                                entity_id,
                                json.loads(
                                    await self.call_builtin_tool(
                                        "__builtin__:ha_get_state",
                                        {"entity_id": entity_id},
                                    )
                                ),
                            )
                        )
                    light_rows: list[tuple[str, dict[str, Any]]] = []
                    indoor_light_entity = self.site_entity("environment.indoor_light")
                    outdoor_radiation_entity = self.site_entity("environment.outdoor_radiation")
                    for entity_id in filter(None, (indoor_light_entity, outdoor_radiation_entity)):
                        light_rows.append(
                            (
                                entity_id,
                                json.loads(
                                    await self.call_builtin_tool(
                                        "__builtin__:ha_get_state",
                                        {"entity_id": entity_id},
                                    )
                                ),
                            )
                        )
                    ac_state = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_get_state",
                            {"entity_id": climate_control_entity},
                        )
                    )
                    power_sensor_entity = self.site_entity("climate.primary.power")
                    power_state = (
                        json.loads(
                            await self.call_builtin_tool(
                                "__builtin__:ha_get_state",
                                {"entity_id": power_sensor_entity},
                            )
                        )
                        if power_sensor_entity
                        else {}
                    )
                    price_entity = self.site_entity("energy.price")
                    pvpc = (
                        json.loads(
                            await self.call_builtin_tool(
                                "__builtin__:ha_get_pvpc_cheapest_hours",
                                {"sensor_entity_id": price_entity, "limit": 3, "include_all_hours": False},
                            )
                        )
                        if price_entity
                        else {}
                    )
                    answer = format_climate_strategy_answer(
                        target_c=float(climate_strategy["target_c"]),
                        daytime=bool(climate_strategy["daytime"]),
                        temp_rows=temp_rows,
                        light_rows=light_rows,
                        ac_state=ac_state,
                        power_state=power_state,
                        pvpc=pvpc,
                        climate_control_entity=climate_control_entity,
                        power_sensor_entity=power_sensor_entity,
                        indoor_light_entity=indoor_light_entity,
                        outdoor_radiation_entity=outdoor_radiation_entity,
                    )
                except Exception as exc:
                    answer = f"No pude preparar la propuesta climática: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

            environment_sensor = requested_environment_sensor(user_text, self.site_profile)
            if environment_sensor and statistical_plan is None:
                entities = list(environment_sensor["entities"])
                label = str(environment_sensor["label"])
                unit = str(environment_sensor["unit"])
                try:
                    state_rows: list[tuple[str, dict[str, Any]]] = []
                    for entity_id in entities:
                        state_rows.append(
                            (
                                entity_id,
                                json.loads(
                                    await self.call_builtin_tool(
                                        "__builtin__:ha_get_state",
                                        {"entity_id": entity_id},
                                    )
                                ),
                            )
                        )
                    answer = format_environment_sensor_answer(label, state_rows, unit)
                except Exception as exc:
                    answer = f"No pude consultar {label}: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

        if task == "homeassistant":
            critical_category = should_offer_critical_completion_alert(user_text)
            if critical_category:
                self.pending_critical_request = user_text
                self.pending_critical_category = critical_category
                answer = (
                    f"Es una operación crítica de {critical_category}. "
                    "¿Quieres que te confirme el resultado y te envíe una alerta al móvil cuando termine? "
                    "Responde sí o no."
                )
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer

        if task == "homeassistant":
            message_batch_answer = self.try_schedule_message_batch(user_text)
            if message_batch_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": message_batch_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, message_batch_answer)
                return message_batch_answer
            scheduled_switch_sequence = self.try_schedule_known_switch_sequence(user_text)
            if scheduled_switch_sequence:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": scheduled_switch_sequence},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, scheduled_switch_sequence)
                return scheduled_switch_sequence
            if not scheduling_intent_hint(user_text):
                light_cycle_answer = await self.try_execute_light_cycle_task(user_text)
                if light_cycle_answer:
                    self.messages.extend(
                        [
                            {"role": "user", "content": conversation_user_text},
                            {"role": "assistant", "content": light_cycle_answer},
                        ]
                    )
                    self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                    await self.learn_from_turn(conversation_user_text, light_cycle_answer)
                    return light_cycle_answer
            cancel_key_answer = self.try_cancel_tasks_by_cancellation_key(user_text)
            if cancel_key_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": cancel_key_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, cancel_key_answer)
                return cancel_key_answer
            kwh_recurring_answer = self.try_schedule_recurring_kwh_mobile_alert(user_text)
            if kwh_recurring_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": kwh_recurring_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, kwh_recurring_answer)
                return kwh_recurring_answer
            power_condition_answer = await self.try_handle_power_threshold_action(user_text)
            if power_condition_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": power_condition_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, power_condition_answer)
                return power_condition_answer
            binary_condition_answer = self.try_schedule_binary_transition(user_text)
            if binary_condition_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": binary_condition_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, binary_condition_answer)
                return binary_condition_answer
            conditional_answer = self.try_schedule_numeric_condition(user_text)
            if conditional_answer:
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": conditional_answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, conditional_answer)
                return conditional_answer
            ac_budget_plan = requested_ac_pvpc_budget_plan(user_text)
            if ac_budget_plan:
                try:
                    payload = json.loads(
                        await self.call_builtin_tool(
                            "__builtin__:ha_plan_ac_pvpc_budget",
                            ac_budget_plan,
                        )
                    )
                    answer = str(payload.get("recommended_answer") or "").strip()
                    if not answer:
                        raise ValueError("El planificador AC-PVPC no devolvio recommended_answer")
                except Exception as exc:
                    answer = f"No pude calcular el plan de presupuesto para el aire acondicionado: {exc}"
                self.messages.extend(
                    [
                        {"role": "user", "content": conversation_user_text},
                        {"role": "assistant", "content": answer},
                    ]
                )
                self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                await self.learn_from_turn(conversation_user_text, answer)
                return answer
            if statistical_plan is None:
                connected_load_answer = await self.try_answer_connected_load_consumption(user_text)
                if connected_load_answer:
                    self.messages.extend(
                        [
                            {"role": "user", "content": conversation_user_text},
                            {"role": "assistant", "content": connected_load_answer},
                        ]
                    )
                    self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                    await self.learn_from_turn(conversation_user_text, connected_load_answer)
                    return connected_load_answer
                historical_comparison_answer = await self.try_answer_numeric_period_comparison(user_text)
                if historical_comparison_answer:
                    self.messages.extend(
                        [
                            {"role": "user", "content": conversation_user_text},
                            {"role": "assistant", "content": historical_comparison_answer},
                        ]
                    )
                    self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                    await self.learn_from_turn(conversation_user_text, historical_comparison_answer)
                    return historical_comparison_answer
                historical_day_answer = await self.try_answer_numeric_weekday_value(user_text)
                if historical_day_answer:
                    self.messages.extend(
                        [
                            {"role": "user", "content": conversation_user_text},
                            {"role": "assistant", "content": historical_day_answer},
                        ]
                    )
                    self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
                    await self.learn_from_turn(conversation_user_text, historical_day_answer)
                    return historical_day_answer

        statistical_period = (
            canonical_statistical_period(statistical_plan)
            if statistical_plan is not None
            else None
        )
        statistical_plan_context = (
            {
                **dataclasses.asdict(statistical_plan),
                "canonical_start_time": statistical_period[0] if statistical_period else "",
                "canonical_end_time": statistical_period[1] if statistical_period else "",
            }
            if statistical_plan is not None
            else None
        )
        sensor_architecture = (
            self.sensor_architecture_context(statistical_plan)
            if statistical_plan is not None
            else None
        )
        working_messages = [
            {"role": "system", "content": self._system_prompt(user_text)},
            *(
                [
                    {
                        "role": "system",
                        "content": (
                            "Plan estadistico semantico ya clasificado:\n"
                            f"{compact_json(statistical_plan_context, 4000)}\n"
                            "Arquitectura local relevante (perfil, catalogo aprendido y relaciones):\n"
                            f"{compact_json(sensor_architecture, 12000)}\n"
                            "Resuelve las entidades por funciones, roles, device_class, unidad, zona, "
                            "aliases y relaciones aprendidas. Usa ha_search_site_entities para el perfil "
                            "semantico y ha_search_entities para completar el catalogo real. El dispositivo "
                            "mencionado no tiene por que ser el sensor que mide la magnitud. Ejecuta la "
                            "herramienta historica adecuada y responde con valor, unidad, periodo, fuentes "
                            "y cobertura. Para maximo/minimo/media de todo el intervalo usa "
                            "ha_aggregate_numeric_history con group_by=period y aplica la consulta a todos "
                            "los sensores semanticos coincidentes, no solo al primero. Los calculos son "
                            "responsabilidad de las herramientas Python/HA; interpreta sus campos numericos "
                            "y no recalcules estadisticas mentalmente. Si la evidencia admite hipotesis "
                            "incompatibles o la conclusion puede provocar una decision de alto impacto, "
                            "termina con una linea ESCALATE_STATISTICS: seguida del motivo concreto. No "
                            "solicites escalado por una consulta rutinaria ni solo por falta de datos."
                        ),
                    }
                ]
                if statistical_plan is not None
                else []
            ),
            *self.messages[-MAX_HISTORY_MESSAGES:],
            {"role": "user", "content": user_text},
        ]

        answer = ""
        used_tools = False
        deterministic_answer: str | None = None
        schedule_intent = bool(scheduling_intent_hint(user_text))
        schedule_retry_sent = False
        scheduled_task_created = False
        scheduled_run_times: set[str] = set()
        single_task_sequence = self.requires_single_scheduled_sequence(user_text)
        inventory_query = task == "homeassistant" and readonly_entity_inventory_intent(user_text)
        inventory_retry_sent = False
        inventory_tool_used = False
        measurement_query = task == "homeassistant" and (
            bool(
                statistical_plan
                and statistical_plan.needs_activity_attribution
            )
            or (
                statistical_plan is None
                and historical_active_device_measurement_intent(user_text)
            )
        )
        measurement_retry_sent = False
        measurement_tool_used = False
        measurement_result: dict[str, Any] | None = None
        measurement_answer_retry_count = 0
        measurement_period_correction = ""
        mobile_alert_allowed = mobile_notification_explicitly_requested(user_text)
        statistical_query = statistical_plan is not None
        statistical_tool_used = False
        statistical_retry_sent = False
        statistical_answer_retry_sent = False
        statistical_escalated = False
        statistical_evidence: list[str] = []
        statistical_semantic_query = (
            statistical_plan.semantic_query if statistical_plan is not None else ""
        )
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._chat(
                working_messages,
                tools=True,
                task="statistical_reasoning" if statistical_query else task,
                requires_memory=True,
            )
            msg = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                answer = msg.content or ""
                if measurement_query and not measurement_tool_used:
                    if measurement_retry_sent:
                        answer = (
                            "No he podido calcular el consumo historico de forma fiable porque "
                            "no se ejecuto la medicion requerida."
                        )
                        working_messages.append({"role": "assistant", "content": answer})
                        break
                    measurement_retry_sent = True
                    working_messages.append({"role": "assistant", "content": answer})
                    working_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "La respuesta anterior esta incompleta: la peticion pide atribuir un consumo "
                                "historico a la actividad de un dispositivo. Resuelve la entidad de actividad "
                                "y el sensor numerico asociado y ejecuta ha_measure_numeric_during_state para "
                                "el periodo solicitado. No respondas solamente con un entity_id. "
                                f"{measurement_period_correction}"
                            ),
                        }
                    )
                    continue
                if (
                    measurement_query
                    and measurement_result is not None
                    and not answer_contains_attributed_measurement(answer, measurement_result)
                ):
                    if measurement_answer_retry_count >= 2:
                        answer = (
                            "No he podido redactar una respuesta estadistica fiable a partir "
                            "del resultado de la medicion."
                        )
                        working_messages.append({"role": "assistant", "content": answer})
                        break
                    measurement_answer_retry_count += 1
                    working_messages.append({"role": "assistant", "content": answer})
                    working_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "La medicion ya se ejecuto correctamente, pero tu respuesta no contesta la "
                                "pregunta. Interpreta el resultado de ha_measure_numeric_during_state que "
                                "figura en esta conversacion y vuelve a redactar libremente la respuesta. "
                                "Incluye el total, la unidad, el periodo y la cobertura; usa los entity_id "
                                "solo como explicacion de las fuentes. No vuelvas a buscar un sensor dedicado."
                            ),
                        }
                    )
                    continue
                if statistical_query and not statistical_tool_used:
                    if statistical_retry_sent:
                        answer = (
                            "No he podido calcular la estadistica porque no se ejecuto "
                            "ninguna herramienta historica valida."
                        )
                        working_messages.append({"role": "assistant", "content": answer})
                        break
                    statistical_retry_sent = True
                    working_messages.append({"role": "assistant", "content": answer})
                    working_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "La respuesta anterior no es valida: el plan estadistico exige consultar "
                                "datos historicos reales. Resuelve las fuentes semanticamente y ejecuta "
                                "ha_aggregate_numeric_history, ha_measure_numeric_during_state, "
                                "ha_count_state_transitions, ha_get_history o ha_get_long_term_statistics "
                                "segun corresponda. No uses el estado actual como estadistica."
                            ),
                        }
                    )
                    continue
                if (
                    statistical_query
                    and statistical_tool_used
                    and (
                        not statistical_answer_is_complete(answer)
                        or not statistical_answer_period_is_consistent(
                            answer, statistical_period
                        )
                    )
                ):
                    if statistical_answer_retry_sent:
                        answer = (
                            "No he podido redactar una respuesta estadistica fiable a partir "
                            "de los resultados historicos."
                        )
                        working_messages.append({"role": "assistant", "content": answer})
                        break
                    statistical_answer_retry_sent = True
                    working_messages.append({"role": "assistant", "content": answer})
                    working_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Interpreta los resultados historicos anteriores y responde a la pregunta. "
                                "Incluye el valor calculado, unidad, periodo, fuentes y cobertura; los "
                                "entity_id solos no son una respuesta estadistica. No menciones fechas "
                                "fuera del intervalo realmente consultado: "
                                f"{statistical_period or 'historico completo'}."
                            ),
                        }
                    )
                    continue
                answer, escalation_reason = extract_statistical_escalation(answer)
                if (
                    statistical_query
                    and escalation_reason
                    and not statistical_escalated
                ):
                    statistical_escalated = True
                    escalation_response = await self._chat(
                        [
                            {
                                "role": "system",
                                "content": (
                                    "Revisa una interpretacion estadistica excepcional. Los calculos ya "
                                    "fueron ejecutados por herramientas: no inventes ni recalcules valores. "
                                    "Contrasta hipotesis, explica la ambiguedad o el impacto y redacta la "
                                    "respuesta final en español con valor, unidad, periodo, fuentes y "
                                    "limitaciones. No propongas acciones fisicas no solicitadas."
                                ),
                            },
                            {
                                "role": "user",
                                "content": (
                                    f"Pregunta: {user_text}\n"
                                    f"Plan: {compact_json(statistical_plan_context, 4000)}\n"
                                    f"Motivo de escalado: {escalation_reason}\n"
                                    "Evidencia calculada:\n"
                                    + "\n".join(statistical_evidence[-6:])
                                    + f"\nPrimera interpretacion: {answer}"
                                ),
                            },
                        ],
                        tools=False,
                        task="statistical_escalation",
                        ignore_request_preference=True,
                    )
                    escalated_answer = (
                        escalation_response.choices[0].message.content or ""
                    ).strip()
                    if (
                        statistical_answer_is_complete(escalated_answer)
                        and statistical_answer_period_is_consistent(
                            escalated_answer, statistical_period
                        )
                    ):
                        answer = escalated_answer
                if inventory_query and not inventory_tool_used:
                    if inventory_retry_sent:
                        answer = (
                            "No puedo dar un recuento fiable sin consultar el inventario semántico "
                            "y los estados reales de Home Assistant."
                        )
                        working_messages.append({"role": "assistant", "content": answer})
                        break
                    inventory_retry_sent = True
                    working_messages.append({"role": "assistant", "content": answer})
                    working_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "La respuesta anterior no es válida porque afirma un inventario sin consultarlo. "
                                "Usa ahora ha_search_site_entities con sólo el concepto y la zona de la petición. "
                                "Es una consulta de solo lectura: no crees tareas, escuchas, automatizaciones ni "
                                "acciones físicas. Después enumera los entity_id devueltos y no inventes nombres."
                            ),
                        }
                    )
                    continue
                if schedule_intent and not scheduled_task_created and not schedule_retry_sent:
                    schedule_retry_sent = True
                    working_messages.append({"role": "assistant", "content": answer})
                    working_messages.append({
                        "role": "user",
                        "content": (
                            "La petición anterior contiene una acción futura, diferida, condicional o recurrente. "
                            "La respuesta no es válida si no creas una tarea o escucha persistente. Para cambios "
                            "de estado/eventos usa create_event_listener; para acciones horarias usa "
                            "schedule_automation; para recordatorios usa schedule_task."
                        ),
                    })
                    continue
                if not answer.strip() and used_tools:
                    answer = await self.summarize_tool_results(working_messages)
                working_messages.append({"role": "assistant", "content": answer})
                break

            working_messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else call
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                used_tools = True
                public_name = call.function.name
                real_name = self.tool_name_map.get(public_name, public_name)
                try:
                    tool_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}

                canonical_range_tools = {
                    "__builtin__:ha_aggregate_numeric_history",
                    "__builtin__:ha_measure_numeric_during_state",
                    "__builtin__:ha_count_state_transitions",
                    "__builtin__:ha_get_history",
                    "__builtin__:ha_get_long_term_statistics",
                    "__builtin__:ha_get_logbook",
                }
                if (
                    statistical_query
                    and statistical_period is not None
                    and real_name in canonical_range_tools
                ):
                    tool_args["start_time"] = statistical_period[0]
                    tool_args["end_time"] = statistical_period[1]
                    if real_name == "__builtin__:ha_aggregate_numeric_history":
                        tool_args["group_by"] = (
                            "day"
                            if statistical_plan
                            and statistical_plan.aggregation == "comparison"
                            else "period"
                        )
                        aggregation_names = {
                            "maximum": "max",
                            "minimum": "min",
                            "average": "mean",
                            "median": "median",
                            "standard_deviation": "stddev",
                            "trend": "trend",
                            "anomaly": "zscore",
                        }
                        if statistical_plan and statistical_plan.aggregation in aggregation_names:
                            tool_args["aggregation"] = aggregation_names[
                                statistical_plan.aggregation
                            ]
                        if statistical_semantic_query and not tool_args.get("entity_id"):
                            tool_args["query"] = statistical_semantic_query

                if (
                    real_name == "__builtin__:ha_send_mobile_alert"
                    and not mobile_alert_allowed
                ):
                    working_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": compact_json(
                                {
                                    "sent": False,
                                    "blocked": True,
                                    "reason": (
                                        "El usuario no pidio explicitamente una notificacion "
                                        "al movil o telefono. Responde por el canal de origen."
                                    ),
                                }
                            ),
                        }
                    )
                    continue

                inventory_read_tools = {
                    "__builtin__:ha_search_site_entities",
                    "__builtin__:ha_search_entities",
                    "__builtin__:ha_get_states",
                    "__builtin__:ha_get_state",
                }
                if inventory_query and real_name in inventory_read_tools:
                    inventory_tool_used = True
                statistical_read_tools = {
                    "__builtin__:ha_aggregate_numeric_history",
                    "__builtin__:ha_measure_numeric_during_state",
                    "__builtin__:ha_count_state_transitions",
                    "__builtin__:ha_get_history",
                    "__builtin__:ha_get_long_term_statistics",
                    "__builtin__:ha_get_logbook",
                    "__builtin__:ha_get_security_activity",
                }
                if inventory_query and (
                    self.is_execution_action_tool(real_name)
                    or real_name
                    in {
                        "__builtin__:schedule_task",
                        "__builtin__:schedule_automation",
                        "__builtin__:create_event_listener",
                    }
                ):
                    content_text = compact_json(
                        {
                            "executed": False,
                            "blocked": True,
                            "reason": (
                                "La petición es un recuento/listado de solo lectura. "
                                "Consulta ha_search_site_entities; no crees tareas ni acciones."
                            ),
                        }
                    )
                    working_messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": content_text}
                    )
                    continue

                if real_name in {
                    "__builtin__:schedule_task",
                    "__builtin__:schedule_automation",
                    "__builtin__:create_event_listener",
                }:
                    if real_name == "__builtin__:create_event_listener":
                        schedule_key = "event:" + compact_json(tool_args, 2000)
                    else:
                        try:
                            schedule_key = parse_datetime_to_utc(str(tool_args.get("run_at") or ""))
                        except Exception:
                            schedule_key = str(tool_args.get("run_at") or "").strip()
                    if schedule_key in scheduled_run_times or (single_task_sequence and scheduled_task_created):
                        content_text = compact_json(
                            {
                                "created": False,
                                "duplicate_ignored": True,
                                "reason": "Esta petición es una única secuencia y ya tiene tarea; combina todas las acciones, esperas y repeticiones en esa tarea.",
                            }
                        )
                        working_messages.append(
                            {"role": "tool", "tool_call_id": call.id, "content": content_text}
                        )
                        continue
                    scheduled_run_times.add(schedule_key)
                    scheduled_task_created = True

                try:
                    if real_name.startswith("__builtin__:"):
                        print(f"\n[interno] {real_name.removeprefix('__builtin__:')} {compact_json(tool_args, 800)}")
                        content_text = await self.call_builtin_tool(real_name, tool_args)
                    else:
                        print(f"\n[MCP] {real_name} {compact_json(tool_args, 800)}")
                        result = await self.session.call_tool(real_name, tool_args)
                        content_text = tool_result_to_text(result) or "(sin contenido)"
                except Exception as exc:  # noqa: BLE001 - se devuelve al modelo como resultado de herramienta
                    content_text = f"ERROR ejecutando {real_name}: {exc}"
                    self.memory.add_event(level="error", message=content_text)

                if (
                    statistical_query
                    and real_name in statistical_read_tools
                    and not content_text.startswith("ERROR")
                ):
                    statistical_tool_used = True
                    statistical_evidence.append(content_text[:16000])
                if (
                    statistical_query
                    and real_name == "__builtin__:ha_search_site_entities"
                ):
                    with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
                        semantic_result = json.loads(content_text)
                        if int(semantic_result.get("count") or 0) > 0:
                            statistical_semantic_query = str(
                                tool_args.get("query") or ""
                            ).strip()

                if (
                    measurement_query
                    and real_name == "__builtin__:ha_measure_numeric_during_state"
                ):
                    parsed_measurement = parse_attributed_measurement_result(content_text)
                    if parsed_measurement is not None:
                        measurement_period_correction = (
                            attributed_measurement_period_error(
                                user_text, parsed_measurement
                            )
                            or ""
                        )
                        if measurement_period_correction:
                            content_text = compact_json(
                                {
                                    "accepted": False,
                                    "reason": measurement_period_correction,
                                    "measurement_result": parsed_measurement,
                                },
                                max_chars=30000,
                            )
                        else:
                            measurement_result = parsed_measurement
                            measurement_tool_used = True

                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": content_text,
                    }
                )
                if real_name == "__builtin__:ha_get_pvpc_cheapest_hours":
                    with contextlib.suppress(Exception):
                        parsed_tool = json.loads(content_text)
                        recommended = str(parsed_tool.get("recommended_answer") or "").strip()
                        if recommended:
                            deterministic_answer = recommended
            if deterministic_answer:
                answer = deterministic_answer
                working_messages.append({"role": "assistant", "content": answer})
                break
        else:
            answer = "He parado la cadena de herramientas porque superó el límite interno."

        self.messages.extend(
            [
                {"role": "user", "content": conversation_user_text},
                {"role": "assistant", "content": answer},
            ]
        )
        self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
        await self.learn_from_turn(conversation_user_text, answer)
        return answer

    @staticmethod
    def requires_single_scheduled_sequence(user_text: str) -> bool:
        folded = fold_accents(user_text.lower())
        has_short_wait = bool(re.search(r"\b(?:espera|esperar)\s+\d+(?:[,.]\d+)?\s*segundos?\b", folded))
        has_repeat = bool(re.search(r"\b(?:repetir|repite|repites|repitelo|repetirlo)\b", folded))
        return has_short_wait and has_repeat and "enc" in folded and "apag" in folded

    def try_cancel_tasks_by_cancellation_key(self, user_text: str) -> str | None:
        key = normalize_cancellation_key(user_text)
        if not key:
            return None
        task_rows = list(
            self.memory.conn.execute(
                """
                SELECT id, title
                FROM tasks
                WHERE status IN ('pending', 'running', 'failed')
                  AND cancellation_key = ?
                ORDER BY run_at ASC, id ASC
                """,
                (key,),
            )
        )
        listener_rows = list(
            self.memory.conn.execute(
                """
                SELECT id, title
                FROM event_subscriptions
                WHERE enabled = 1 AND cancellation_key = ?
                ORDER BY id ASC
                """,
                (key,),
            )
        )
        if not task_rows and not listener_rows:
            return None
        now = utc_now()
        cancelled_rows: list[sqlite3.Row] = []
        for row in task_rows:
            task_id = int(row["id"])
            if self.memory.cancel_task(task_id):
                self.memory.conn.execute(
                    "UPDATE tasks SET updated_at = ?, result = ?, last_error = NULL WHERE id = ? AND status = 'cancelled'",
                    (now, f"Cancelada por clave: {key}", task_id),
                )
                self.memory.conn.commit()
                cancelled_rows.append(row)
        cancelled_listeners = [
            row
            for row in listener_rows
            if set_subscription_enabled(self.memory.conn, int(row["id"]), False)
        ]
        if not cancelled_rows and not cancelled_listeners:
            return None
        parts: list[str] = []
        if cancelled_rows:
            parts.append(
                "tareas " + ", ".join(f"#{int(row['id'])}" for row in cancelled_rows)
            )
        if cancelled_listeners:
            parts.append(
                "escuchas "
                + ", ".join(f"#{int(row['id'])}" for row in cancelled_listeners)
            )
        return f"He cancelado las {' y '.join(parts)} asociadas a la clave '{key}'."

    def load_phrase_triggers(self) -> list[dict[str, Any]]:
        raw = self.memory.get_setting(PHRASE_TRIGGERS_SETTING, "[]")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    def save_phrase_triggers(self, triggers: list[dict[str, Any]]) -> None:
        self.memory.set_setting(
            PHRASE_TRIGGERS_SETTING,
            json.dumps(triggers, ensure_ascii=False, separators=(",", ":")),
        )

    def register_phrase_trigger(self, trigger: dict[str, Any]) -> None:
        phrase = str(trigger.get("phrase") or "").strip()
        if not phrase:
            raise ValueError("phrase es obligatorio")
        normalized = normalize_cancellation_key(phrase)
        trigger = dict(trigger)
        trigger["phrase"] = phrase
        trigger["normalized_phrase"] = normalized
        trigger.setdefault("id", str(uuid.uuid4()))
        triggers = [
            item for item in self.load_phrase_triggers()
            if normalize_cancellation_key(str(item.get("phrase") or item.get("normalized_phrase") or "")) != normalized
        ]
        triggers.append(trigger)
        self.save_phrase_triggers(triggers)

    def activate_phrase_trigger(self, user_text: str) -> str | None:
        key = normalize_cancellation_key(user_text)
        if not key:
            return None
        triggers = self.load_phrase_triggers()
        matched = next(
            (
                item for item in triggers
                if normalize_cancellation_key(str(item.get("normalized_phrase") or item.get("phrase") or "")) == key
            ),
            None,
        )
        if not matched:
            return None

        batch = dict(matched.get("batch") or {})
        count = max(1, min(int(batch.get("count") or 1), 100))
        spacing_seconds = max(0.0, min(float(batch.get("spacing_seconds") or 0), 86400.0))
        expires_after_seconds = batch.get("expires_after_seconds")
        expires_after = None if expires_after_seconds in (None, "", 0, "0") else max(1.0, float(expires_after_seconds))
        cancellation_key = str(matched.get("cancellation_key") or "").strip() or None
        priority = max(0, min(int(matched.get("priority") or 50), 100))
        title_template = str(batch.get("title_template") or matched.get("title") or "Trigger {phrase} {index}/{total}")
        plan_template = batch.get("plan")
        if not isinstance(plan_template, dict):
            raise ValueError("El trigger por frase requiere batch.plan")

        started_at = dt.datetime.now(dt.UTC)
        expires_at = started_at + dt.timedelta(seconds=expires_after) if expires_after else None
        created: list[tuple[int, float]] = []
        for offset_index in range(count):
            index = offset_index + 1
            run_at = started_at + dt.timedelta(seconds=offset_index * spacing_seconds)
            plan = json.loads(json.dumps(plan_template, ensure_ascii=False))
            plan["name"] = str(plan.get("name") or title_template).format(
                phrase=matched.get("phrase") or user_text,
                index=index,
                total=count,
            )
            if expires_at is not None:
                plan["expires_at"] = expires_at.isoformat(timespec="microseconds")
            task_id = self.memory.add_task(
                run_at=run_at.isoformat(timespec="seconds"),
                title=title_template.format(
                    phrase=matched.get("phrase") or user_text,
                    index=index,
                    total=count,
                ),
                instruction=encode_plan(plan),
                cancellation_key=cancellation_key,
                priority=priority,
                max_attempts=1,
            )
            created.append((task_id, offset_index * spacing_seconds))

        if matched.get("once", True):
            self.save_phrase_triggers([item for item in triggers if item is not matched])

        ids = ", ".join(f"#{task_id}@+{offset:g}s" for task_id, offset in created)
        expiry = f" Caducan a los {expires_after:g}s." if expires_after else ""
        cancel = f" Cancelación: '{cancellation_key}'." if cancellation_key else ""
        return f"Trigger '{matched.get('phrase')}' activado: {len(created)} tareas encoladas. {ids}.{expiry}{cancel}"

    def try_schedule_message_batch(self, user_text: str) -> str | None:
        folded = normalize_cancellation_key(user_text)
        if not re.search(r"\b(?:tarea|tareas|trabajo|trabajos|job|jobs)\b", folded):
            return None
        match = re.search(
            r"\b(?:manda|crea|programa|agenda|pon|lanza)?\s*"
            r"(?P<count>\d{1,3})\s+(?:tareas?|trabajos?|jobs?)\b"
            r"(?P<body>.+?)\b(?:hasta\s+que\s+(?:yo\s+)?(?:diga|mande|escriba)\s+"
            r"(?P<cancel>['\"“”‘’]?[\wáéíóúüñÁÉÍÓÚÜÑ ]+['\"“”‘’]?))?\s*$",
            user_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        count = max(1, min(int(match.group("count")), 100))
        body = re.sub(r"\s+", " ", match.group("body")).strip()
        interval_match = re.search(
            r"\bcada\s+(?:(?P<half>medio|media)\s+)?(?:(?P<value>\d{1,5})\s*)?"
            r"(?P<unit>segundos?|segs?|s|minutos?|mins?|m|horas?|h)\b",
            body,
            flags=re.IGNORECASE,
        )
        if not interval_match:
            return None
        half = bool(interval_match.group("half"))
        unit = normalize_cancellation_key(interval_match.group("unit"))
        interval_value = float(interval_match.group("value") or (0.5 if half else 1))
        if unit.startswith("h"):
            interval_seconds = int(interval_value * 3600)
        elif unit.startswith("m") and unit not in {"ms"}:
            interval_seconds = int(interval_value * 60)
        else:
            interval_seconds = int(interval_value)
        interval_seconds = max(1, min(interval_seconds, 86400))
        action_text = (body[: interval_match.start()] + body[interval_match.end() :]).strip()
        message_match = re.search(
            r"\b(?:que\s+)?(?:digan|diga|di|decir|saquen|saque|muestren|muestra|impriman|imprime)\s+(?P<what>.+)$",
            action_text,
            flags=re.IGNORECASE,
        )
        if not message_match:
            return None
        what = re.sub(r"\s+", " ", message_match.group("what")).strip().strip("'\"“”‘’. ")
        what_folded = normalize_cancellation_key(what)
        if what_folded in {"la fecha", "fecha", "el dia", "dia"}:
            step = {
                "type": "builtin",
                "name": "say_time",
                "args": {"timezone": DEFAULT_TIMEZONE, "message": "Fecha: {date}"},
            }
            title_prefix = "Fecha"
            detail = "fecha"
        elif what_folded in {"la hora", "hora"}:
            step = {
                "type": "builtin",
                "name": "say_time",
                "args": {"timezone": DEFAULT_TIMEZONE, "message": "Hora: {time}"},
            }
            title_prefix = "Hora"
            detail = "hora"
        else:
            if not what:
                return None
            step = {
                "type": "builtin",
                "name": "terminal_message",
                "args": {"message": what},
            }
            title_prefix = "Mensaje"
            detail = repr(what)
        cancellation_key = None
        if match.group("cancel"):
            cancellation_key = normalize_cancellation_key(match.group("cancel").strip().strip("'\"“”‘’. "))
        started_at = dt.datetime.now(dt.UTC)
        created: list[tuple[int, int]] = []
        for offset_index in range(count):
            index = offset_index + 1
            plan = {
                "version": 1,
                "name": f"{title_prefix} {index:02d}/{count}",
                "conditions": [],
                "steps": [step],
            }
            offset = offset_index * interval_seconds
            task_id = self.memory.add_task(
                run_at=(started_at + dt.timedelta(seconds=offset)).isoformat(timespec="seconds"),
                title=f"{title_prefix} {index:02d}/{count}",
                instruction=encode_plan(plan),
                cancellation_key=cancellation_key,
                priority=60,
                max_attempts=1,
            )
            created.append((task_id, offset))
        ids = ", ".join(f"#{task_id}@+{offset}s" for task_id, offset in created)
        cancel = f" Cancelación: '{cancellation_key}'." if cancellation_key else ""
        return (
            f"He encolado {count} trabajos cada {interval_seconds}s: {ids}."
            f" Acción: sacar {detail}.{cancel}"
        )

    def try_schedule_numeric_condition(self, user_text: str) -> str | None:
        compiled = compile_numeric_condition(
            user_text,
            resolve_sensor=lambda query: self.resolve_catalog_entity(query, domain="sensor"),
            resolve_action=self.resolve_site_alias,
        )
        if compiled is None:
            return None
        plan = attach_completion_notification(compiled.plan, user_text)
        run_at = (local_now() + dt.timedelta(seconds=compiled.delay_seconds)).isoformat(timespec="seconds")
        task_id = self.memory.add_task(
            run_at=run_at,
            title=compiled.title,
            instruction=encode_plan(plan),
        )
        return f"He creado la tarea condicional generica #{task_id}: {compiled.summary}."

    def try_schedule_binary_transition(self, user_text: str) -> str | None:
        started_at = dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")
        compiled = compile_binary_transition(
            user_text,
            start_time=started_at,
            resolve_sensor=lambda query: self.resolve_catalog_entity(query, domain="binary_sensor"),
            resolve_action=self.resolve_site_alias,
        )
        if compiled is None:
            return None
        plan = attach_completion_notification(compiled.plan, user_text)
        run_at = (local_now() + dt.timedelta(seconds=compiled.delay_seconds)).isoformat(timespec="seconds")
        task_id = self.memory.add_task(
            run_at=run_at,
            title=compiled.title,
            instruction=encode_plan(plan),
        )
        return f"He creado la tarea de transicion generica #{task_id}: {compiled.summary}."

    def resolve_catalog_entity(self, query: str, *, domain: str) -> str | None:
        folded = fold_accents(query.lower())
        explicit = re.search(rf"\b{re.escape(domain)}\.[a-z0-9_]+\b", folded)
        if explicit:
            return explicit.group(0)
        candidates = self.memory.search_entity_catalog(query, domain=domain, limit=3)
        return str(candidates[0]["entity_id"]) if candidates else None

    def try_correct_recent_scheduled_task(self, user_text: str) -> str | None:
        if not requested_recent_task_correction(user_text):
            return None
        row = self.memory.conn.execute(
            """
            SELECT id, run_at, title, instruction, created_at
            FROM tasks
            WHERE status = 'pending'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return "No encuentro una tarea pendiente que corregir."

        task_id = int(row["id"])
        old_instruction = str(row["instruction"] or "")
        tts_payload = corrected_tts_payload(user_text)
        if tts_payload:
            media_match = re.search(r"media_player\.[a-z0-9_]+", old_instruction)
            if media_match:
                tts_payload["media_player_entity_id"] = media_match.group(0)
            instruction = "DETERMINISTIC_TTS_REMINDER " + json.dumps(tts_payload, ensure_ascii=False)
            message = str(tts_payload["message"])
            title = f"Decir {message}" + (" y hora" if tts_payload.get("include_time") else "")
        else:
            instruction = (
                "Correccion de tarea pendiente. Ejecuta esta instruccion actualizada y no crees otra tarea: "
                + user_text.strip()
            )
            title = str(row["title"] or "Tarea corregida")

        updated = self.memory.update_task(task_id=task_id, title=title, instruction=instruction)
        if not updated:
            return f"No pude corregir la tarea #{task_id}; puede que ya no esté pendiente."
        run_at_local = dt.datetime.fromisoformat(str(row["run_at"])).astimezone(ZoneInfo(DEFAULT_TIMEZONE))
        return (
            f"He corregido la tarea pendiente #{task_id} sin crear otra nueva. "
            f"Mantiene la hora {run_at_local.strftime('%H:%M')} y ahora ejecutará: {title}."
        )

    def try_schedule_recurring_kwh_mobile_alert(self, user_text: str) -> str | None:
        folded = fold_accents(user_text.lower())
        if not any(term in folded for term in ("precio", "kwh", "kw h", "kw/h", "luz barata", "luz cara", "precio de la luz")):
            return None
        if not any(term in folded for term in ("movil", "notifica", "notificacion", "avisame", "mandame", "manda", "alerta")):
            return None
        if not any(term in folded for term in ("cada hora", "horario", "cada 1 hora", "cada una hora")):
            return None
        if not any(term in folded for term in ("hasta", "cancele", "cancelar", "nueva orden", "diga lo contrario")):
            return None

        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        next_hour = (now.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).astimezone(dt.UTC)
        payload = {
            "sensor_entity_id": "sensor.pvpc_dh",
            "notify": "notify/mobile_app_sm_a566b",
            "title": "Precio kWh",
        }
        instruction = "DETERMINISTIC_KWH_MOBILE_ALERT " + json.dumps(payload, ensure_ascii=False)
        previous_rows = list(
            self.memory.conn.execute(
                """
                SELECT id FROM tasks
                WHERE status IN ('pending','running','failed') AND title = ?
                """,
                ("Aviso horario precio kWh al movil",),
            )
        )
        for row in previous_rows:
            previous_id = int(row["id"])
            if self.memory.cancel_task(previous_id):
                self.memory.conn.execute(
                    """
                    UPDATE tasks SET result = 'Cancelada por reemplazo: nueva tarea horaria de precio kWh.',
                        last_error = NULL
                    WHERE id = ? AND status = 'cancelled'
                    """,
                    (previous_id,),
                )
        task_id = self.memory.add_task(
            run_at=next_hour.isoformat(timespec="seconds"),
            title="Aviso horario precio kWh al movil",
            instruction=instruction,
            interval_seconds=3600,
            priority=80,
        )
        self.memory.conn.commit()
        return (
            f"He creado la tarea recurrente #{task_id}: mandar el precio del kWh al móvil cada hora "
            f"hasta que la canceles. Próxima ejecución: {next_hour.isoformat(timespec='seconds')}."
        )

    def resolve_power_sensor_from_catalog(self, subject: str) -> dict[str, Any] | None:
        subject_folded = fold_accents(subject.lower())
        subject_tokens = tokenize_search_query(subject_folded)
        if not subject_tokens:
            return None

        relation_rows = list(
            self.memory.conn.execute(
                """
                SELECT content
                FROM memories
                WHERE topic = 'electricidad_consumo'
                ORDER BY id DESC
                LIMIT 100
                """
            )
        )
        relation_scores: list[tuple[int, str]] = []
        for relation_row in relation_rows:
            content = str(relation_row["content"] if isinstance(relation_row, sqlite3.Row) else relation_row[0])
            match = re.search(
                r"sensor\.[\w_]+ es el sensor de consumo acumulado \(.+?\) del grupo (?P<group>.+?); su sensor de potencia instantanea relacionado es (?P<power>sensor\.[\w_]+)",
                content,
            )
            if not match:
                continue
            group_folded = fold_accents(match.group("group").lower())
            score = sum(3 for token in subject_tokens if token in group_folded)
            if score:
                relation_scores.append((score, match.group("power")))
        if relation_scores:
            relation_scores.sort(key=lambda item: item[0], reverse=True)
            power_entity = relation_scores[0][1]
            row = self.memory.conn.execute(
                """
                SELECT entity_id, friendly_name, area_name, aliases, state, last_changed
                FROM entity_catalog
                WHERE entity_id = ?
                """,
                (power_entity,),
            ).fetchone()
            if row:
                return dict(row)
            return {"entity_id": power_entity, "friendly_name": None, "area_name": subject, "aliases": "[]", "state": None, "last_changed": None}

        rows = list(
            self.memory.conn.execute(
                """
                SELECT entity_id, friendly_name, area_name, aliases, state, last_changed
                FROM entity_catalog
                WHERE unit = 'W' OR device_class = 'power' OR aliases LIKE '%potencia%' OR aliases LIKE '%watios%'
                """
            )
        )
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            aliases = ""
            with contextlib.suppress(Exception):
                aliases = " ".join(json.loads(row["aliases"] or "[]"))
            haystack = fold_accents(f"{row['entity_id']} {row['friendly_name'] or ''} {row['area_name'] or ''} {aliases}".lower())
            score = sum(3 for token in subject_tokens if token in fold_accents(str(row["area_name"] or "").lower()))
            score += sum(2 for token in subject_tokens if token in fold_accents(aliases.lower()))
            score += sum(1 for token in subject_tokens if token in haystack)
            if score:
                scored.append((score, dict(row)))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1] if scored else None

    async def try_handle_power_threshold_action(self, user_text: str) -> str | None:
        folded = fold_accents(user_text.lower())
        if "potencia" not in folded and "watios" not in folded and "consumo ahora" not in folded:
            return None
        if not any(word in folded for word in ("baja", "baje", "menor", "inferior", "debajo")):
            return None
        if not any(word in folded for word in ("enciende", "encender", "activa", "activar")):
            return None

        threshold_match = re.search(r"(?P<threshold>\d+(?:[,.]\d+)?)\s*w\b", folded)
        subject_match = re.search(
            r"(?:potencia|watios|consumo ahora)\s+(?:de\s+la|del|de\s+los|de\s+las|de)?\s*(?P<subject>.+?)\s+(?:baja|baje|menor|inferior|por debajo|debajo)",
            folded,
        )
        if not threshold_match or not subject_match:
            return None
        subject = subject_match.group("subject").strip()
        threshold = float(threshold_match.group("threshold").replace(",", "."))
        power_row = self.resolve_power_sensor_from_catalog(subject)
        if not power_row:
            return None

        if "cocina" in folded and "luz" in folded:
            action_domain = "switch"
            action_service = "turn_on"
            action_entity = "switch.nspanel_relay_2"
            action_label = "luz de cocina"
        else:
            return None

        payload = {
            "sensor_entity_id": power_row["entity_id"],
            "subject": subject,
            "operator": "<",
            "threshold": threshold,
            "action_domain": action_domain,
            "action_service": action_service,
            "action_entity_id": action_entity,
            "action_label": action_label,
            "delay_minutes": 1,
        }
        return await self.evaluate_power_threshold_action(payload, create_task_if_pending=True)

    async def evaluate_power_threshold_action(self, payload: dict[str, Any], *, create_task_if_pending: bool = False) -> str:
        sensor_entity = str(payload["sensor_entity_id"])
        threshold = float(payload["threshold"])
        state_text = await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": sensor_entity})
        state_data = json.loads(state_text)
        raw_state = state_data.get("state")
        try:
            value = float(raw_state)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"No puedo convertir la potencia de {sensor_entity} a numero: {raw_state}") from exc

        action_entity = str(payload["action_entity_id"])
        action_domain = str(payload["action_domain"])
        action_service = str(payload["action_service"])
        action_label = str(payload.get("action_label") or action_entity)
        subject = str(payload.get("subject") or sensor_entity)
        if value < threshold:
            await self.call_builtin_tool(
                "__builtin__:ha_call_service",
                {
                    "domain": action_domain,
                    "service": action_service,
                    "target": {"entity_id": action_entity},
                    "confirm": True,
                },
            )
            verify_text = await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": action_entity})
            verify = json.loads(verify_text)
            return (
                f"La potencia de {subject} ({sensor_entity}) esta en {value:.1f} W, por debajo de {threshold:.0f} W. "
                f"He ejecutado {action_domain}.{action_service} sobre {action_label} ({action_entity}); estado final {verify.get('state')}."
            )

        operator_map = {"<": "lt", "<=": "lte", ">": "gt", ">=": "gte", "==": "eq", "!=": "ne"}
        automation_plan = {
            "version": 1,
            "name": f"Potencia {subject} {payload.get('operator', '<')} {threshold:g} W",
            "conditions": [
                {
                    "entity_id": sensor_entity,
                    "operator": operator_map.get(str(payload.get("operator") or "<"), "lt"),
                    "value": threshold,
                }
            ],
            "condition_policy": {
                "on_false": "reschedule",
                "delay_seconds": int(payload.get("delay_minutes", 1) or 1) * 60,
            },
            "steps": [
                {
                    "type": "service",
                    "domain": action_domain,
                    "service": action_service,
                    "target": {"entity_id": action_entity},
                    "expected_state": "on" if action_service == "turn_on" else "off",
                }
            ],
        }
        task_instruction = encode_plan(automation_plan)
        if create_task_if_pending:
            run_at = (local_now() + dt.timedelta(minutes=int(payload.get("delay_minutes", 1) or 1))).isoformat(timespec="seconds")
            task_id = self.memory.add_task(
                run_at=run_at,
                title=f"Condicion potencia {subject} < {threshold:.0f} W -> {action_label}",
                instruction=task_instruction,
            )
            return (
                f"La potencia de {subject} ({sensor_entity}) esta en {value:.1f} W, aun no baja de {threshold:.0f} W. "
                f"He creado la tarea condicional #{task_id} para comprobarlo cada minuto y encender {action_label} cuando se cumpla."
            )

        await self.reschedule_current_task(
            {
                "delay_minutes": int(payload.get("delay_minutes", 1) or 1),
                "title": f"Condicion potencia {subject} < {threshold:.0f} W -> {action_label}",
                "instruction": task_instruction,
                "reason": f"Potencia actual {value:.1f} W, umbral {threshold:.0f} W",
            }
        )
        return f"Condición pendiente: {sensor_entity} esta en {value:.1f} W; reprogramada hasta que baje de {threshold:.0f} W."

    async def try_answer_connected_load_consumption(self, user_text: str) -> str | None:
        folded = fold_accents(user_text.lower())
        if not any(word in folded for word in ("consume", "consumiendo", "consumo", "potencia", "watios", "watio", "gasta", "gastando")):
            return None

        memories = list(
            self.memory.conn.execute(
                """
                SELECT content
                FROM memories
                WHERE topic = 'electricidad_elementos_conectados'
                ORDER BY id DESC
                LIMIT 50
                """
            )
        )
        for row in memories:
            content = str(row["content"] if isinstance(row, sqlite3.Row) else row[0])
            match = re.search(
                r"El grupo electrico (?P<group>.+?) medido por (?P<energy>sensor\.[\w_]+) incluye elementos conectados: (?P<items>.+?)\. Usar",
                content,
            )
            if not match:
                continue
            group = match.group("group").strip()
            energy_entity = match.group("energy").strip()
            items = [item.strip() for item in match.group("items").split(",") if item.strip()]
            matched_item = next((item for item in items if fold_accents(item.lower()) in folded), None)
            if not matched_item:
                continue

            power_row = self.memory.conn.execute(
                """
                SELECT entity_id, friendly_name, unit, state, last_changed
                FROM entity_catalog
                WHERE area_name = ? AND unit = 'W'
                ORDER BY entity_id
                LIMIT 1
                """,
                (group,),
            ).fetchone()
            if not power_row:
                return (
                    f"{matched_item} pertenece al grupo electrico {group}, medido en consumo acumulado por {energy_entity}, "
                    "pero no encuentro sensor de potencia instantanea asociado en el catalogo."
                )

            power_entity = str(power_row["entity_id"])
            state = str(power_row["state"] or "unknown")
            last_changed = str(power_row["last_changed"] or "")
            with contextlib.suppress(Exception):
                state_text = await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": power_entity})
                state_data = json.loads(state_text)
                state = str(state_data.get("state") or state)
                last_changed = str(state_data.get("last_changed") or last_changed)

            try:
                watts = float(state)
                state_text_value = f"{watts:.1f} W"
            except (TypeError, ValueError):
                state_text_value = state

            connected = ", ".join(items)
            return (
                f"La lavadora no tiene sensor propio catalogado; esta dentro del grupo {group}. "
                f"El sensor instantaneo del grupo es {power_entity} y ahora marca {state_text_value}. "
                f"Ese valor es del grupo completo, no solo de la lavadora. Elementos conectados: {connected}."
                + (f" Ultimo cambio: {last_changed}." if last_changed else "")
            )
        return None

    def rank_numeric_history_sensors(self, user_text: str) -> list[tuple[sqlite3.Row, str]]:
        folded = fold_accents(user_text.lower())
        candidates = self.memory.search_entity_catalog(user_text, domain="sensor", limit=20)
        query_tokens = set(tokenize_search_query(folded))
        requested_classes = requested_numeric_device_classes(query_tokens)
        scope_tokens = numeric_history_scope_tokens(query_tokens, SPANISH_MONTH_NAMES)
        ranked: list[tuple[int, sqlite3.Row, str]] = []
        for row in candidates:
            aliases = ""
            with contextlib.suppress(Exception):
                aliases = " ".join(json.loads(row["aliases"] or "[]"))
            haystack = fold_accents(
                f"{row['entity_id']} {row['friendly_name'] or ''} {row['device_class'] or ''} {row['unit'] or ''} {aliases}".lower()
            )
            score = sum(2 for token in query_tokens if token in haystack)
            scope_matches = sum(1 for token in scope_tokens if token in haystack)
            if scope_tokens:
                score += scope_matches * 12
                if not scope_matches:
                    score -= 20
            device_class = str(row["device_class"] or "").lower()
            if requested_classes and device_class not in requested_classes:
                continue
            if query_tokens.intersection(NUMERIC_HISTORY_CLASS_TERMS.get(device_class, set())):
                score += 20
            if "dia" in query_tokens and any(term in haystack for term in ("diario", "diaria", "daily")):
                score += 8
            try:
                float(row["state"])
            except (TypeError, ValueError):
                continue
            if score:
                ranked.append((score, row, haystack))
        if not ranked:
            return []
        ranked.sort(key=lambda item: item[0], reverse=True)
        ordered = [(sensor, haystack) for _, sensor, haystack in ranked]
        primary_id = str(ordered[0][0]["entity_id"])
        if primary_id.endswith("_diario"):
            cumulative_id = primary_id.removesuffix("_diario")
            ordered.sort(key=lambda item: 0 if str(item[0]["entity_id"]) == primary_id else 1 if str(item[0]["entity_id"]) == cumulative_id else 2)
        return ordered

    def resolve_numeric_history_sensor(self, user_text: str) -> tuple[sqlite3.Row, str] | None:
        ranked = self.rank_numeric_history_sensors(user_text)
        return ranked[0] if ranked else None

    async def numeric_sensor_value_for_period(
        self,
        candidates: list[tuple[sqlite3.Row, str]],
        *,
        start: dt.datetime,
        end: dt.datetime,
        consumption_query: bool,
    ) -> tuple[sqlite3.Row, float, str] | None:
        for sensor, sensor_haystack in candidates:
            resets_daily = any(term in sensor_haystack for term in ("diario", "diaria", "daily"))
            aggregation = "max" if consumption_query and resets_daily else "delta" if consumption_query else "max"
            raw = await self.call_builtin_tool(
                "__builtin__:ha_aggregate_numeric_history",
                {
                    "entity_id": str(sensor["entity_id"]),
                    "start_time": start.isoformat(timespec="seconds"),
                    "end_time": end.isoformat(timespec="seconds"),
                    "timezone": str(start.tzinfo),
                    "group_by": "day",
                    "aggregation": aggregation,
                    "exclude_start_state": aggregation != "delta",
                },
            )
            payload = json.loads(raw)
            results = payload.get("results") or []
            periods = results[0].get("periods") if results else []
            if periods:
                unit = str(results[0].get("unit") or sensor["unit"] or "").strip()
                return sensor, float(periods[0]["value"]), unit

        for sensor, _sensor_haystack in candidates:
            raw = await self.call_builtin_tool(
                "__builtin__:ha_get_long_term_statistics",
                {
                    "entity_id": str(sensor["entity_id"]),
                    "start_time": start.isoformat(timespec="seconds"),
                    "end_time": end.isoformat(timespec="seconds"),
                },
            )
            payload = json.loads(raw)
            if payload.get("value") is not None:
                return sensor, float(payload["value"]), str(sensor["unit"] or "").strip()
        return None

    async def try_answer_numeric_period_comparison(self, user_text: str) -> str | None:
        folded = fold_accents(user_text.lower())
        if not re.search(r"\b(?:que|cual)\s+dia\b", folded):
            return None
        if not any(term in folded for term in ("semana pasada", "semana anterior")):
            return None
        wants_highest = any(term in folded for term in (" mas ", "mayor", "maximo", "maxima"))
        wants_lowest = any(term in folded for term in (" menos ", "menor", "minimo", "minima"))
        if not wants_highest and not wants_lowest:
            return None

        now = local_now()
        start, end = previous_calendar_week_range(now)
        explicit_water_query = is_explicit_water_query(folded)
        energy_query = is_energy_query(folded, explicit_water_query=explicit_water_query)
        requested_scope = requested_energy_supply_scope(user_text) if energy_query else None
        if energy_query and requested_scope is None:
            candidates = self.rank_numeric_history_sensors(user_text)
            supply_groups: dict[str, list[tuple[sqlite3.Row, str]]] = {}
            for candidate in candidates:
                group = energy_supply_group(str(candidate[0]["entity_id"]))
                if group:
                    supply_groups.setdefault(group, []).append(candidate)
            daily_totals: list[tuple[dt.date, float, str, list[tuple[str, float]]]] = []
            for offset in range((end.date() - start.date()).days):
                day = start.date() + dt.timedelta(days=offset)
                day_start = dt.datetime.combine(day, dt.time.min, tzinfo=now.tzinfo)
                day_end = day_start + dt.timedelta(days=1)
                group_values: list[tuple[str, float]] = []
                unit = ""
                for group in ENERGY_SUPPLY_GROUP_LABELS:
                    selected = await self.numeric_sensor_value_for_period(
                        supply_groups.get(group) or [],
                        start=day_start,
                        end=day_end,
                        consumption_query=True,
                    )
                    if selected:
                        _sensor, value, selected_unit = selected
                        group_values.append((group, value))
                        unit = unit or selected_unit
                if group_values:
                    daily_totals.append((day, sum(value for _, value in group_values), unit, group_values))
            if daily_totals:
                selected_day, value, unit, group_values = (
                    max(daily_totals, key=lambda item: item[1])
                    if wants_highest
                    else min(daily_totals, key=lambda item: item[1])
                )
                weekday_name = next(name for name, index in SPANISH_WEEKDAY_INDEX.items() if index == selected_day.weekday())
                details = ", ".join(
                    f"{ENERGY_SUPPLY_GROUP_LABELS[group]} {group_value:.3f}".replace(".", ",")
                    for group, group_value in group_values
                )
                comparison = "mayor" if wants_highest else "menor"
                return (
                    f"El {weekday_name} {selected_day.day} de {SPANISH_MONTH_NAMES[selected_day.month - 1]} fue el dia "
                    f"de {comparison} consumo total suministrado de la semana pasada: "
                    f"{f'{value:.2f}'.replace('.', ',')}{f' {unit}' if unit else ''}. "
                    f"Suma de grupos: {details}{f' {unit}' if unit else ''}. "
                    f"Periodo comparado: {start.date().isoformat()} a {(end.date() - dt.timedelta(days=1)).isoformat()}."
                )

        resolved = self.resolve_numeric_history_sensor(user_text)
        if not resolved:
            return None
        sensor, sensor_haystack = resolved

        consumption_query = is_consumption_query(folded)
        resets_daily = any(term in sensor_haystack for term in ("diario", "diaria", "daily"))
        if consumption_query and not resets_daily:
            aggregation = "delta"
        elif wants_lowest and not consumption_query:
            aggregation = "min"
        else:
            aggregation = "max"
        raw = await self.call_builtin_tool(
            "__builtin__:ha_aggregate_numeric_history",
            {
                "entity_id": str(sensor["entity_id"]),
                "start_time": start.isoformat(timespec="seconds"),
                "end_time": end.isoformat(timespec="seconds"),
                "timezone": str(now.tzinfo),
                "group_by": "day",
                "aggregation": aggregation,
                "exclude_start_state": aggregation != "delta",
            },
        )
        payload = json.loads(raw)
        results = payload.get("results") or []
        if not results:
            return None
        result = results[0]
        period = result.get("highest" if wants_highest else "lowest")
        if not period:
            return None
        day = dt.date.fromisoformat(str(period["period"]))
        value = float(period["value"])
        value_text = f"{value:.2f}".replace(".", ",")
        unit = str(result.get("unit") or sensor["unit"] or "").strip()
        comparison = "mayor" if wants_highest else "menor"
        metric = "consumo" if consumption_query else "valor"
        sensor_label = str(sensor["friendly_name"] or sensor["entity_id"])
        return (
            f"El {period.get('weekday_name') or day.strftime('%A')} {day.day} de {SPANISH_MONTH_NAMES[day.month - 1]} "
            f"fue el dia de {comparison} {metric} de la semana pasada: {value_text}{f' {unit}' if unit else ''}. "
            f"Medido por {sensor_label} ({sensor['entity_id']}). "
            f"Periodo comparado: {start.date().isoformat()} a {(end.date() - dt.timedelta(days=1)).isoformat()}."
        )

    async def try_answer_numeric_weekday_value(self, user_text: str) -> str | None:
        folded = fold_accents(user_text.lower())
        if scheduling_intent_hint(user_text):
            return None
        if not is_numeric_consumption_query(folded):
            return None

        now = local_now()
        day = requested_history_date(user_text, now)
        if day is None:
            return None
        candidates = self.rank_numeric_history_sensors(user_text)
        if not candidates:
            return None
        start = dt.datetime.combine(day, dt.time.min, tzinfo=now.tzinfo)
        end = start + dt.timedelta(days=1)
        consumption_query = is_numeric_consumption_query(folded)
        explicit_water_query = is_explicit_water_query(folded)
        energy_query = is_energy_query(folded, explicit_water_query=explicit_water_query)
        requested_scope = requested_energy_supply_scope(user_text) if energy_query else None

        supply_groups: dict[str, list[tuple[sqlite3.Row, str]]] = {}
        if energy_query:
            for candidate in candidates:
                group = energy_supply_group(str(candidate[0]["entity_id"]))
                if group:
                    supply_groups.setdefault(group, []).append(candidate)

        if energy_query and requested_scope is None and supply_groups:
            values: list[tuple[str, sqlite3.Row, float, str]] = []
            for group in ENERGY_SUPPLY_GROUP_LABELS:
                group_candidates = supply_groups.get(group) or []
                selected = await self.numeric_sensor_value_for_period(
                    group_candidates,
                    start=start,
                    end=end,
                    consumption_query=True,
                )
                if selected:
                    sensor, value, unit = selected
                    values.append((group, sensor, value, unit))
            if not values:
                return None
            total = sum(value for _, _, value, _ in values)
            unit = values[0][3]
            weekday_name = next(name for name, index in SPANISH_WEEKDAY_INDEX.items() if index == day.weekday())
            details = ", ".join(
                f"{ENERGY_SUPPLY_GROUP_LABELS[group]} {value:.3f}".replace(".", ",")
                for group, _sensor, value, _unit in values
            )
            missing = [label for group, label in ENERGY_SUPPLY_GROUP_LABELS.items() if group not in {item[0] for item in values}]
            completeness = "" if not missing else f" No hubo datos para: {', '.join(missing)}."
            return (
                f"El consumo total suministrado del {weekday_name} {day.day} de {SPANISH_MONTH_NAMES[day.month - 1]} "
                f"fue de {f'{total:.2f}'.replace('.', ',')}{f' {unit}' if unit else ''}. "
                f"Suma de grupos: {details}{f' {unit}' if unit else ''}.{completeness}"
            )

        scoped_candidates = candidates
        if energy_query and requested_scope:
            scoped_candidates = [
                candidate
                for candidate in candidates
                if energy_supply_group(str(candidate[0]["entity_id"])) == requested_scope
            ]
        selected = await self.numeric_sensor_value_for_period(
            scoped_candidates,
            start=start,
            end=end,
            consumption_query=consumption_query,
        )
        if not selected:
            return None
        sensor, value, unit = selected
        value_text = f"{value:.2f}".replace(".", ",")
        sensor_label = str(sensor["friendly_name"] or sensor["entity_id"])
        weekday_name = next(name for name, index in SPANISH_WEEKDAY_INDEX.items() if index == day.weekday())
        return (
            f"El consumo del {weekday_name} {day.day} de {SPANISH_MONTH_NAMES[day.month - 1]} fue de "
            f"{value_text}{f' {unit}' if unit else ''}. Medido por {sensor_label} ({sensor['entity_id']})."
        )

    async def summarize_tool_results(self, working_messages: list[dict[str, Any]]) -> str:
        response = await self._chat(
            [
                *working_messages,
                {
                    "role": "user",
                    "content": (
                        "Resume los resultados de las herramientas anteriores y contesta a la pregunta original. "
                        "Si hay datos numéricos, incluye entidad, valor, unidad y hora."
                    ),
                },
            ],
            tools=False,
            task="summary",
        )
        return response.choices[0].message.content or "He consultado las herramientas, pero no pude generar un resumen."

    async def execute_scheduled_instruction(self, task_id: int, run_id: int, title: str, instruction: str) -> str:
        self.current_task_id = task_id
        self.current_task_run_id = run_id
        self.current_task_mobile_alert_sent = False
        if explicit_whatsapp_send_intent(instruction):
            prompt = (
                "Ejecuta ahora esta tarea de mensajeria. Usa exclusivamente whatsapp_send_message "
                "para enviar el texto y destinatario indicados. No ejecutes acciones de Home Assistant, "
                "riego, automatizaciones ni avisos moviles aunque el contenido del mensaje mencione una "
                "accion fisica. El contenido citado es texto para WhatsApp, no una orden domotica. "
                "No llames ha_send_mobile_alert. Responde brevemente confirmando el envio.\n"
                f"Tarea #{task_id}: {title}\n"
                f"Instruccion: {instruction}"
            )
            try:
                async with self.ask_lock:
                    return await self._run_task_unlocked(
                        prompt,
                        require_tool=True,
                        require_action=True,
                        allow_mobile_alert=False,
                    )
            finally:
                self.current_task_id = None
                self.current_task_run_id = None
        automation_result = await self.try_execute_automation_plan(instruction)
        if automation_result is not None:
            self.current_task_id = None
            self.current_task_run_id = None
            return automation_result
        light_cycle_result = await self.try_execute_light_cycle_task(instruction)
        if light_cycle_result is not None:
            self.current_task_id = None
            self.current_task_run_id = None
            return light_cycle_result
        known_action_result = await self.try_execute_known_switch_action_task(instruction)
        if known_action_result is not None:
            self.current_task_id = None
            self.current_task_run_id = None
            return known_action_result
        deterministic_result = await self.try_execute_transition_condition_task(title, instruction)
        if deterministic_result is not None:
            self.current_task_id = None
            self.current_task_run_id = None
            return deterministic_result
        ac_power_result = await self.try_execute_ac_power_control_task(instruction)
        if ac_power_result is not None:
            self.current_task_id = None
            self.current_task_run_id = None
            return ac_power_result
        water_liters_result = await self.try_execute_water_liters_task(instruction)
        if water_liters_result is not None:
            self.current_task_id = None
            self.current_task_run_id = None
            return water_liters_result
        tts_reminder_result = await self.try_execute_tts_reminder_task(instruction)
        if tts_reminder_result is not None:
            self.current_task_id = None
            self.current_task_run_id = None
            return tts_reminder_result
        kwh_alert_result = await self.try_execute_kwh_mobile_alert_task(instruction)
        if kwh_alert_result is not None:
            self.current_task_id = None
            self.current_task_run_id = None
            return kwh_alert_result

        requires_tool = self.requires_execution_tool(instruction)
        lowered_instruction = instruction.lower()
        whatsapp_read_only = (
            any(
                marker in lowered_instruction
                for marker in (
                    "whatsapp_list_contacts",
                    "whatsapp_get_status",
                    "whatsapp_get_recent_messages",
                )
            )
            and "whatsapp_send_message" not in lowered_instruction
        )
        requires_action = requires_tool and not whatsapp_read_only
        simple_message = None if requires_tool else self.simple_reminder_message(instruction)
        if simple_message:
            print(f"\n[recordatorio #{task_id}] {simple_message}")
            self.current_task_id = None
            self.current_task_run_id = None
            return simple_message

        prompt = (
            "Ejecuta ahora esta tarea programada: su hora ya ha llegado. "
            "No interpretes tiempos relativos dentro de la instrucción (por ejemplo 'en 1 minuto', 'dentro de 5 minutos') "
            "como una orden nueva de programar o reprogramar; son contexto original de cuándo debía ejecutarse. "
            "No crees tareas duplicadas. No uses schedule_task ni reschedule_current_task para demorar la acción salvo que "
            "la tarea sea claramente condicional ('cuando...', 'si...', 'hasta que...') y la condición todavía no se cumpla. "
            "Si una tarea condicional todavía no se cumple, es obligatorio llamar reschedule_current_task; no basta con decir en texto que la reprogramas. "
            "Si la condición se cumple, ejecuta la acción y termina. "
            "Si la tarea pide cambiar un dispositivo de Home Assistant, debes usar ha_call_service con confirm=true "
            "y despues comprobar el estado con ha_get_state. Si la tarea pide esperar unos segundos entre acciones, usa wait_seconds y despues continua con los pasos restantes. No des por ejecutada una accion sin herramienta. "
            "No hagas preguntas ni esperes una respuesta del usuario durante una tarea agendada. La confirmacion se resolvio al crearla. No envies alertas moviles salvo que la instruccion pida explicitamente una notificacion al movil o telefono. "
            "Responde muy breve indicando qué hiciste.\n"
            f"Tarea #{task_id}: {title}\n"
            f"Instrucción: {instruction}"
        )
        try:
            async with self.ask_lock:
                return await self._run_task_unlocked(
                    prompt,
                    require_tool=requires_tool,
                    require_action=requires_action,
                    allow_mobile_alert=mobile_notification_explicitly_requested(instruction),
                )
        finally:
            self.current_task_id = None
            self.current_task_run_id = None

    def try_schedule_known_switch_sequence(self, user_text: str) -> str | None:
        compiled = compile_timed_alternation(
            user_text,
            resolve_entity=self.resolve_site_alias,
        )
        if compiled is None:
            return None
        plan = attach_completion_notification(compiled.plan, user_text)
        run_at = (local_now() + dt.timedelta(seconds=compiled.delay_seconds)).isoformat(timespec="seconds")
        task_id = self.memory.add_task(
            run_at=run_at,
            title=compiled.title,
            instruction=encode_plan(plan),
        )
        return f"He creado la tarea #{task_id}: {compiled.summary}."

    async def try_execute_automation_plan(self, instruction: str) -> str | None:
        plan = decode_plan(instruction) or decode_legacy_plan(instruction)
        if plan is None:
            return None
        outcome = await AutomationExecutor(self.call_builtin_tool).execute(plan)
        updated_instruction = encode_plan(outcome.updated_plan) if outcome.updated_plan else instruction
        if outcome.status == "reschedule":
            await self.reschedule_current_task(
                {
                    "delay_seconds": outcome.reschedule_seconds,
                    "instruction": updated_instruction,
                    "reason": outcome.message,
                }
            )
            return f"{outcome.message} Tarea reprogramada."
        if outcome.updated_plan and getattr(self, "current_task_id", None) is not None:
            self.memory.update_running_task_instruction(
                self.current_task_id,
                self.worker_id,
                updated_instruction,
            )
        return outcome.message

    async def try_execute_light_cycle_task(self, instruction: str) -> str | None:
        folded = fold_accents(instruction.lower())
        if not all(word in folded for word in ("enc", "apag", "segund")):
            return None

        resolved = self.resolve_site_alias(instruction)
        if resolved is None:
            return None

        repeat_match = re.search(
            r"(?:repetir|repite|repites|repitelo|repetirlo)[^\d]{0,30}(\d+)\s+veces",
            folded,
        )
        wait_match = re.search(r"(\d+(?:[,.]\d+)?)\s*segundos?", folded)
        if not wait_match:
            return None

        cycles = max(1, min(int(repeat_match.group(1)), 20)) if repeat_match else 1
        wait_seconds = max(0.1, min(float(wait_match.group(1).replace(",", ".")), 60.0))
        entity_id, label = resolved
        final_state_is_off = False

        try:
            for cycle in range(1, cycles + 1):
                final_state_is_off = False
                cycle_started = time.monotonic()
                await self.call_builtin_tool(
                    "__builtin__:ha_call_service",
                    {
                        "domain": "switch",
                        "service": "turn_on",
                        "target": {"entity_id": entity_id},
                        "confirm": True,
                    },
                )
                on_state = json.loads(
                    await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": entity_id})
                ).get("state")
                if on_state != "on":
                    raise RuntimeError(f"El ciclo {cycle} no pudo encender {entity_id}; estado observado: {on_state}")

                remaining_wait = max(0.0, wait_seconds - (time.monotonic() - cycle_started))
                if remaining_wait > 0:
                    await self.call_builtin_tool("__builtin__:wait_seconds", {"seconds": remaining_wait})
                await self.call_builtin_tool(
                    "__builtin__:ha_call_service",
                    {
                        "domain": "switch",
                        "service": "turn_off",
                        "target": {"entity_id": entity_id},
                        "confirm": True,
                    },
                )
                off_state = json.loads(
                    await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": entity_id})
                ).get("state")
                if off_state != "off":
                    raise RuntimeError(f"El ciclo {cycle} no pudo apagar {entity_id}; estado observado: {off_state}")
                final_state_is_off = True
        finally:
            if not final_state_is_off:
                with contextlib.suppress(Exception):
                    await self.call_builtin_tool(
                        "__builtin__:ha_call_service",
                        {
                            "domain": "switch",
                            "service": "turn_off",
                            "target": {"entity_id": entity_id},
                            "confirm": True,
                        },
                    )

        wait_text = f"{wait_seconds:g} segundos"
        cycle_text = "1 ciclo" if cycles == 1 else f"{cycles} ciclos"
        return f"Ejecutados {cycle_text} de {label} ({entity_id}), encendida {wait_text} en cada ciclo; estado final off."

    async def try_execute_known_switch_action_task(self, instruction: str) -> str | None:
        if instruction.lstrip().startswith(("DETERMINISTIC_", "AUTOMATION_PLAN_V1")):
            return None
        resolved = self.resolve_site_alias(instruction)
        if resolved is None:
            return None

        folded = fold_accents(instruction.lower())
        turn_on = any(word in folded for word in ("enciende", "encender", "activa", "activar"))
        turn_off = any(word in folded for word in ("apaga", "apagar", "desactiva", "desactivar"))
        if turn_on == turn_off:
            return None

        entity_id, label = resolved
        if "." not in entity_id:
            return None
        entity_domain = entity_id.split(".", 1)[0]
        service = "turn_on" if turn_on else "turn_off"
        expected_state = "on" if turn_on else "off"
        action_text = await self.call_builtin_tool(
            "__builtin__:ha_call_service",
            {
                "domain": entity_domain,
                "service": service,
                "target": {"entity_id": entity_id},
                "confirm": True,
            },
        )
        try:
            action_result = json.loads(action_text)
        except json.JSONDecodeError:
            action_result = {}
        observed_state = json.loads(
            await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": entity_id})
        ).get("state")
        if observed_state != expected_state:
            raise RuntimeError(
                f"No se pudo ejecutar {entity_domain}.{service} sobre {entity_id}; estado observado: {observed_state}"
            )
        power_verification = action_result.get("power_verification")
        if isinstance(power_verification, dict) and not power_verification.get("verified"):
            raise RuntimeError(
                f"{entity_domain}.{service} dejó {entity_id} en {observed_state}, pero no se verificó "
                f"el cambio de potencia: {json.dumps(power_verification, ensure_ascii=False)[:700]}"
            )
        power_text = " Potencia verificada." if isinstance(power_verification, dict) else ""
        return f"Ejecutado {entity_domain}.{service} sobre {label} ({entity_id}); estado final {observed_state}.{power_text}"

    async def try_execute_kwh_mobile_alert_task(self, instruction: str) -> str | None:
        prefix = "DETERMINISTIC_KWH_MOBILE_ALERT "
        if not instruction.strip().startswith(prefix):
            return None
        payload = json.loads(instruction.strip()[len(prefix) :])
        sensor_entity = str(payload.get("sensor_entity_id") or "sensor.pvpc_dh")
        title = str(payload.get("title") or "Precio kWh")
        notify = str(payload.get("notify") or "notify/mobile_app_sm_a566b")
        state_text = await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": sensor_entity})
        state = json.loads(state_text)
        attrs = state.get("attributes") or {}
        raw_price = state.get("state")
        try:
            price = float(raw_price)
            price_text = f"{price:.5f} EUR/kWh ({price * 100:.2f} cent/kWh)"
        except (TypeError, ValueError):
            price_text = str(raw_price)
        period = attrs.get("period")
        min_price = attrs.get("min_price")
        min_at = attrs.get("min_price_at")
        message = f"Precio kWh ahora: {price_text}"
        if period:
            message += f". Periodo: {period}"
        if min_price is not None and min_at is not None:
            with contextlib.suppress(Exception):
                message += f". Minimo hoy: {float(min_price) * 100:.2f} cent/kWh a las {int(min_at):02d}:00"
        await self.call_builtin_tool(
            "__builtin__:ha_send_mobile_alert",
            {
                "title": title,
                "message": message,
                "notify": notify,
                "volume": 100,
                "media_stream": "alarm_stream",
            },
        )
        return f"Notificación móvil enviada: {message}"

    def parse_water_liters_payload(self, instruction: str) -> dict[str, Any] | None:
        text = instruction.strip()
        prefix = "DETERMINISTIC_WATER_LITERS "
        if text.startswith(prefix):
            payload = json.loads(text[len(prefix) :])
            if not isinstance(payload, dict):
                raise ValueError("DETERMINISTIC_WATER_LITERS requiere un objeto JSON")
            return payload
        folded = fold_accents(text.lower())
        if not any(
            term in folded
            for term in (
                "riego",
                "regar",
                "riega",
                "echa",
                "echale",
                "dale",
                "pon",
                "ponle",
                "tira",
                "tirale",
            )
        ):
            return None
        if "litro" not in folded and " l" not in folded:
            return None
        amount_match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:l|litro|litros)\b", folded)
        if not amount_match:
            return None
        switch_matches = re.findall(r"\bswitch\.[a-z0-9_]+\b", folded)
        meter_match = re.search(r"\b(?:sensor|input_number)\.[a-z0-9_]+\b", folded)
        amount_value = float(amount_match.group(1).replace(",", "."))
        amount_text = f"{amount_value:g}"
        switch_entity_id = self.select_water_zone_switch(switch_matches) or self.default_water_switch_for_instruction(folded)
        meter_entity_id = meter_match.group(0) if meter_match else self.site_entity("irrigation.flow_meter")
        if not switch_entity_id or not meter_entity_id:
            return None
        pass_valve = self.site_entity("irrigation.pass_valve")
        alarm_entity_ids = list(self.site_entities("irrigation.alarms"))
        payload = {
            "switch_entity_id": switch_entity_id,
            "meter_entity_id": meter_entity_id,
            "target_liters": amount_value,
            "check_interval_seconds": 10,
            "max_runtime_seconds": 3600,
            "max_no_progress_seconds": 0,
            "required_valve_states": {pass_valve: "off"} if pass_valve else {},
            "alarm_entity_ids": alarm_entity_ids,
            "start_alert": {
                "title": "Riego",
                "message": f"Riego de {amount_text} litros iniciado",
            },
            "completion_alert": {
                "title": "Riego",
                "message": f"Riego de {amount_text} litros completado",
            },
            "failure_alert": {
                "title": "Fallo riego",
                "message": f"Riego de {amount_text} litros detenido por seguridad",
            },
        }
        master_switch = self.default_water_master_switch_for_zone(switch_entity_id)
        if master_switch:
            payload["master_switch_entity_id"] = master_switch
        appdaemon_manual = self.appdaemon_manual_water_config_for_switch(switch_entity_id)
        if appdaemon_manual:
            payload["appdaemon_manual"] = appdaemon_manual
        return payload

    def try_schedule_whatsapp_water_liters(self, instruction: str) -> str | None:
        if "[Contexto del canal: mensaje entrante de WhatsApp" not in instruction:
            return None
        payload = self.parse_water_liters_payload(instruction)
        if payload is None:
            return None
        folded = fold_accents(instruction.lower())
        if not re.search(
            r"\b(?:riega|regar|echa|echale|dale|pon|ponle|tira|tirale|"
            r"inicia(?:r)?\s+(?:el\s+)?riego)\b",
            folded,
        ):
            return None
        target_liters = float(payload["target_liters"])
        switch_entity = str(payload["switch_entity_id"])
        role_data = self.site_profile.role_for_entity(switch_entity, "irrigation.")
        zone_label = (
            str(role_data[1].get("label") or switch_entity)
            if role_data
            else switch_entity
        )
        for key in ("start_alert", "completion_alert", "failure_alert"):
            alert = dict(payload.get(key) or {})
            alert["whatsapp"] = True
            alert["mobile_enabled"] = False
            payload[key] = alert
        task_id = self.memory.add_task(
            run_at=utc_now(),
            title=f"Riego WhatsApp: {zone_label}, {target_liters:g} litros",
            instruction=(
                "DETERMINISTIC_WATER_LITERS "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
            cancellation_key=f"riego-whatsapp-{switch_entity}",
            priority=90,
        )
        return (
            f"He creado la tarea #{task_id}: {zone_label}, {target_liters:g} litros. "
            "Te avisaré por WhatsApp al iniciar y al terminar o si se detiene por seguridad."
        )

    def select_water_zone_switch(self, switch_entity_ids: list[str]) -> str | None:
        if not switch_entity_ids:
            return None
        for entity_id in switch_entity_ids:
            if self.appdaemon_manual_water_config_for_switch(entity_id):
                return entity_id
        master = self.site_entity("irrigation.primary.master")
        return next((entity_id for entity_id in switch_entity_ids if entity_id != master), switch_entity_ids[0])

    def default_water_switch_for_instruction(self, folded_instruction: str) -> str | None:
        profile = getattr(self, "site_profile", None)
        resolved = profile.resolve_alias(folded_instruction, "irrigation.") if profile is not None else None
        if resolved:
            role_data = profile.role_for_entity(resolved[0], "irrigation.")
            if role_data and role_data[1].get("kind") == "zone":
                return resolved[0]
        if profile is not None:
            for role, binding in profile.roles.items():
                if role.startswith("irrigation.") and binding.get("kind") == "zone" and binding.get("default"):
                    return profile.entity(role)
        return None

    def default_water_master_switch_for_zone(self, switch_entity_id: str) -> str | None:
        profile = getattr(self, "site_profile", None)
        role_data = profile.role_for_entity(switch_entity_id, "irrigation.") if profile is not None else None
        if not role_data:
            return None
        master_role = role_data[1].get("master_role")
        return self.site_entity(str(master_role)) if master_role else None

    def appdaemon_manual_water_config_for_switch(self, switch_entity_id: str) -> dict[str, Any] | None:
        profile = getattr(self, "site_profile", None)
        role_data = profile.role_for_entity(switch_entity_id, "irrigation.") if profile is not None else None
        if not role_data:
            return None
        controller = role_data[1].get("controller")
        if not isinstance(controller, dict):
            return None
        if set(controller) == {"programador", "zone"}:
            return self.appdaemon_manual_water_config(
                str(controller["programador"]),
                int(controller["zone"]),
            )
        return dict(controller)

    @staticmethod
    def appdaemon_manual_water_config(programador: str, zone: int) -> dict[str, Any]:
        if programador not in {"riego", "riego2"}:
            raise ValueError("programador de riego no soportado")
        if zone < 1 or zone > 8:
            raise ValueError("zona de riego fuera de rango")
        current_prefix = "riego2_riego_actual" if programador == "riego2" else "riego_actual"
        remaining_prefix = "riego2_restante" if programador == "riego2" else "riego_restante"
        return {
            "programador": programador,
            "zone": zone,
            "activation_mode_entity_id": f"input_select.{programador}_modo_activacion_zona{zone}",
            "quantity_mode_entity_id": f"input_select.{programador}_modo_cantidad_zona{zone}",
            "target_liters_entity_id": f"input_number.{programador}_litros_zona{zone}",
            "manual_button_entity_id": f"input_button.{programador}_manual_zona{zone}",
            "actual_sensor_entity_id": f"sensor.{current_prefix}_zona{zone}",
            "remaining_sensor_entity_id": f"sensor.{remaining_prefix}_zona{zone}",
            "restore_target_liters": True,
        }

    def normalize_water_liters_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        switch_entity = str(payload.get("switch_entity_id") or "").strip()
        appdaemon_manual = payload.get("appdaemon_manual")
        can_migrate_to_appdaemon = payload.get("baseline_liters") is None
        if not isinstance(appdaemon_manual, dict) and can_migrate_to_appdaemon:
            appdaemon_manual = self.appdaemon_manual_water_config_for_switch(switch_entity)
            if appdaemon_manual:
                payload["appdaemon_manual"] = appdaemon_manual
        if isinstance(payload.get("appdaemon_manual"), dict):
            master_switch = self.default_water_master_switch_for_zone(switch_entity)
            if master_switch and not payload.get("master_switch_entity_id"):
                payload["master_switch_entity_id"] = master_switch
            if (
                payload.get("baseline_liters") is None
                and payload.get("meter_entity_id") == "input_number.lectura_total_compensada_caudalimetro"
            ):
                payload["meter_entity_id"] = "sensor.controlh2oficina_acumulado_temporal_caudalimetro"
            power_verification = payload.get("power_verification")
            if isinstance(power_verification, dict):
                power_verification["enabled"] = False
                power_verification["disabled_reason"] = "AppDaemon por caudal verifica el riego; piaoficina_power no esta calibrado para esta zona."
            elif power_verification is not False:
                payload["power_verification"] = {"enabled": False}
        return payload

    async def get_entity_state(self, entity_id: str) -> str:
        return str(
            json.loads(await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": entity_id})).get("state")
        )

    async def set_water_switches_state(self, switch_entity: str, service: str, *, master_switch: str | None = None) -> None:
        domain = switch_entity.split(".", 1)[0]
        if service == "turn_on" and master_switch and master_switch != switch_entity:
            master_state = await self.get_entity_state(master_switch)
            if master_state.strip().lower() != "on":
                await self.call_builtin_tool(
                    "__builtin__:ha_call_service",
                    {
                        "domain": master_switch.split(".", 1)[0],
                        "service": "turn_on",
                        "target": {"entity_id": master_switch},
                        "confirm": True,
                    },
                )
        await self.call_builtin_tool(
            "__builtin__:ha_call_service",
            {
                "domain": domain,
                "service": service,
                "target": {"entity_id": switch_entity},
                "confirm": True,
            },
        )
        if service == "turn_off" and master_switch and master_switch != switch_entity:
            await self.call_builtin_tool(
                "__builtin__:ha_call_service",
                {
                    "domain": master_switch.split(".", 1)[0],
                    "service": "turn_off",
                    "target": {"entity_id": master_switch},
                    "confirm": True,
                },
            )

    async def wait_water_power_verification(self, payload: dict[str, Any], switch_entity: str, master_switch: str | None) -> None:
        verification = payload.get("power_verification") or {}
        if verification is False or verification.get("enabled") is False:
            return
        sensor_entity = str(verification.get("sensor_entity_id") or "sensor.piaoficina_power").strip()
        min_watts = float(verification.get("min_watts") or 300)
        wait_seconds = max(0.0, min(float(verification.get("wait_seconds") or 3), 30.0))
        max_attempts = max(1, min(int(verification.get("max_attempts") or 2), 3))
        last_power: float | None = None
        last_switch_state = ""
        for attempt in range(1, max_attempts + 1):
            if wait_seconds:
                await self.call_builtin_tool("__builtin__:wait_seconds", {"seconds": wait_seconds})
            last_switch_state = await self.get_entity_state(switch_entity)
            raw_power = await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": sensor_entity})
            last_power = self.numeric_state_from_json(raw_power, entity_id=sensor_entity)
            if last_switch_state.strip().lower() == "on" and last_power >= min_watts:
                return
            if attempt < max_attempts:
                await self.set_water_switches_state(switch_entity, "turn_on", master_switch=master_switch)
        await self.set_water_switches_state(switch_entity, "turn_off", master_switch=master_switch)
        await self.send_water_task_alert(
            payload,
            "failure_alert",
            (
                f"Riego no verificado: {switch_entity} quedó {last_switch_state!r} y "
                f"{sensor_entity} marca {last_power if last_power is not None else 'desconocido'} W; "
                f"se esperaba al menos {min_watts:g} W."
            ),
        )
        raise RuntimeError(
            f"Riego no verificado: {switch_entity}={last_switch_state!r}, "
            f"{sensor_entity}={last_power} W; mínimo esperado {min_watts:g} W."
        )

    async def call_water_input_select(self, entity_id: str, option: str) -> None:
        await self.call_builtin_tool(
            "__builtin__:ha_call_service",
            {
                "domain": "input_select",
                "service": "select_option",
                "target": {"entity_id": entity_id},
                "service_data": {"option": option},
                "confirm": True,
            },
        )

    async def call_water_input_number(self, entity_id: str, value: float) -> None:
        await self.call_builtin_tool(
            "__builtin__:ha_call_service",
            {
                "domain": "input_number",
                "service": "set_value",
                "target": {"entity_id": entity_id},
                "service_data": {"value": value},
                "confirm": True,
            },
        )

    async def press_water_manual_button(self, entity_id: str) -> None:
        await self.call_builtin_tool(
            "__builtin__:ha_call_service",
            {
                "domain": "input_button",
                "service": "press",
                "target": {"entity_id": entity_id},
                "confirm": True,
            },
        )

    async def prepare_appdaemon_manual_water(self, payload: dict[str, Any], target_liters: float) -> None:
        manual = payload.get("appdaemon_manual")
        if not isinstance(manual, dict) or payload.get("appdaemon_manual_started"):
            return
        target_entity = str(manual.get("target_liters_entity_id") or "").strip()
        if target_entity and "previous_target_liters" not in manual:
            try:
                manual["previous_target_liters"] = self.numeric_state_from_json(
                    await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": target_entity}),
                    entity_id=target_entity,
                )
            except RuntimeError:
                manual["previous_target_liters"] = None
        await self.call_water_input_select(str(manual["activation_mode_entity_id"]), "Manual")
        await self.call_water_input_select(str(manual["quantity_mode_entity_id"]), "Por caudal")
        await self.call_water_input_number(target_entity, target_liters)
        await self.press_water_manual_button(str(manual["manual_button_entity_id"]))
        payload["appdaemon_manual_started"] = True
        payload["appdaemon_manual_started_at"] = utc_now()
        await self.call_builtin_tool("__builtin__:wait_seconds", {"seconds": 3})

    async def restore_appdaemon_manual_water_target(self, payload: dict[str, Any]) -> None:
        manual = payload.get("appdaemon_manual")
        if not isinstance(manual, dict) or not manual.get("restore_target_liters"):
            return
        if payload.get("appdaemon_manual_target_restored"):
            return
        previous = manual.get("previous_target_liters")
        target_entity = str(manual.get("target_liters_entity_id") or "").strip()
        if previous is None or not target_entity:
            return
        await self.call_water_input_number(target_entity, float(previous))
        payload["appdaemon_manual_target_restored"] = True

    async def appdaemon_water_state(self, payload: dict[str, Any]) -> tuple[str | None, float | None]:
        manual = payload.get("appdaemon_manual")
        if not isinstance(manual, dict):
            return None, None
        actual_entity = str(manual.get("actual_sensor_entity_id") or "").strip()
        remaining_entity = str(manual.get("remaining_sensor_entity_id") or "").strip()
        actual_state = None
        remaining = None
        if actual_entity:
            actual_state = str(
                json.loads(await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": actual_entity})).get("state")
            )
        if remaining_entity:
            try:
                remaining = self.numeric_state_from_json(
                    await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": remaining_entity}),
                    entity_id=remaining_entity,
                )
            except RuntimeError:
                remaining = None
        return actual_state, remaining

    @staticmethod
    def numeric_state_from_json(raw: str, *, entity_id: str) -> float:
        data = json.loads(raw)
        state = data.get("state")
        try:
            return float(state)
        except (TypeError, ValueError) as exc:
            match = re.search(r"-?\d+(?:[,.]\d+)?", str(state or ""))
            if match:
                return float(match.group(0).replace(",", "."))
            raise RuntimeError(f"{entity_id} no tiene estado numérico válido: {state!r}") from exc

    async def send_water_task_alert(self, payload: dict[str, Any], key: str, default_message: str) -> None:
        alert = dict(payload.get(key) or {})
        if alert.get("enabled") is False:
            return
        alert.setdefault("title", "Riego")
        alert.setdefault("message", default_message)
        if alert.pop("whatsapp", False):
            await self.call_builtin_tool(
                "__builtin__:whatsapp_send_message",
                {
                    "to": "last_sender",
                    "message": str(alert["message"]),
                },
            )
        if alert.pop("mobile_enabled", True) is False:
            return
        alert.setdefault("notify", "notify/mobile_app_sm_a566b")
        alert.setdefault("volume", 100)
        alert.setdefault("media_stream", "alarm_stream")
        alert.setdefault("speak", True)
        alert.setdefault("critical", True)
        await self.call_builtin_tool("__builtin__:ha_send_mobile_alert", alert)

    async def try_execute_water_liters_task(self, instruction: str) -> str | None:
        payload = self.parse_water_liters_payload(instruction)
        if payload is None:
            return None
        payload = self.normalize_water_liters_payload(payload)

        switch_entity = str(payload.get("switch_entity_id") or "switch.riego_rele2").strip()
        master_switch = str(
            payload.get("master_switch_entity_id") or self.default_water_master_switch_for_zone(switch_entity) or ""
        ).strip() or None
        if master_switch:
            payload["master_switch_entity_id"] = master_switch
        meter_entity = str(payload.get("meter_entity_id") or "input_number.lectura_total_compensada_caudalimetro").strip()
        target_liters = max(0.1, min(float(payload.get("target_liters") or 0), 10000.0))
        check_interval = max(2.0, min(float(payload.get("check_interval_seconds") or 10), 300.0))
        max_runtime = max(30.0, min(float(payload.get("max_runtime_seconds") or 3600), 86400.0))
        appdaemon_manual = payload.get("appdaemon_manual") if isinstance(payload.get("appdaemon_manual"), dict) else None
        raw_no_progress = payload.get("max_no_progress_seconds")
        max_no_progress = 0.0 if raw_no_progress in (None, "", 0, "0", False) else max(30.0, min(float(raw_no_progress), max_runtime))
        alarm_entities = [
            str(item).strip()
            for item in payload.get("alarm_entity_ids") or []
            if str(item).strip()
        ]
        required_valve_states = payload.get("required_valve_states")
        if required_valve_states is None:
            required_valve_states = {"switch.controlh2oficina_relealmacen4": "off"}
        if not isinstance(required_valve_states, dict):
            raise ValueError("required_valve_states debe ser un objeto entity_id -> estado esperado")

        current = self.numeric_state_from_json(
            await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": meter_entity}),
            entity_id=meter_entity,
        )
        now_iso = utc_now()
        baseline = payload.get("baseline_liters")
        if baseline is None:
            for valve_entity, expected_state in required_valve_states.items():
                valve_entity_id = str(valve_entity).strip()
                expected = str(expected_state).strip().lower()
                observed = json.loads(
                    await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": valve_entity_id})
                ).get("state")
                if str(observed).strip().lower() != expected:
                    await self.set_water_switches_state(switch_entity, "turn_off", master_switch=master_switch)
                    await self.send_water_task_alert(
                        payload,
                        "failure_alert",
                        (
                            f"Riego no iniciado: llave de paso {valve_entity_id} está {observed!r}; "
                            f"debe estar {expected!r}."
                        ),
                    )
                    raise RuntimeError(
                        f"Riego no iniciado: llave de paso {valve_entity_id} está {observed!r}; debe estar {expected!r}."
                    )
            baseline = current
            payload["baseline_liters"] = baseline
            payload["started_at"] = now_iso
            payload["last_liters"] = current
            payload["last_progress_at"] = now_iso
            if not payload.get("start_notified"):
                await self.send_water_task_alert(
                    payload,
                    "start_alert",
                    f"Riego iniciado hasta {target_liters:g} litros.",
                )
                payload["start_notified"] = True
            if appdaemon_manual:
                await self.prepare_appdaemon_manual_water(payload, target_liters)
            else:
                await self.set_water_switches_state(switch_entity, "turn_on", master_switch=master_switch)
                await self.wait_water_power_verification(payload, switch_entity, master_switch)
        baseline = float(baseline)
        target_value = baseline + target_liters
        delivered = max(0.0, current - baseline)
        last_liters = float(payload.get("last_liters") if payload.get("last_liters") is not None else baseline)
        if current > last_liters + 0.05:
            payload["last_liters"] = current
            payload["last_progress_at"] = now_iso

        for alarm_entity in alarm_entities:
            alarm_state = json.loads(
                await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": alarm_entity})
            ).get("state")
            if str(alarm_state).lower() == "on":
                await self.set_water_switches_state(switch_entity, "turn_off", master_switch=master_switch)
                await self.restore_appdaemon_manual_water_target(payload)
                await self.send_water_task_alert(
                    payload,
                    "failure_alert",
                    f"Riego detenido: alarma activa {alarm_entity}. Entregados {delivered:.1f}/{target_liters:g} L.",
                )
                raise RuntimeError(
                    f"Riego detenido por alarma {alarm_entity}; entregados {delivered:.1f}/{target_liters:g} L."
                )

        started_at = dt.datetime.fromisoformat(str(payload.get("started_at") or now_iso).replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.UTC)
        elapsed = (dt.datetime.now(dt.UTC) - started_at.astimezone(dt.UTC)).total_seconds()
        if elapsed > max_runtime and delivered < target_liters:
            await self.set_water_switches_state(switch_entity, "turn_off", master_switch=master_switch)
            await self.restore_appdaemon_manual_water_target(payload)
            await self.send_water_task_alert(
                payload,
                "failure_alert",
                f"Riego detenido por tiempo máximo. Entregados {delivered:.1f}/{target_liters:g} L.",
            )
            raise RuntimeError(
                f"Riego detenido por tiempo máximo; entregados {delivered:.1f}/{target_liters:g} L."
            )
        last_progress_at = dt.datetime.fromisoformat(str(payload.get("last_progress_at") or payload.get("started_at") or now_iso).replace("Z", "+00:00"))
        if last_progress_at.tzinfo is None:
            last_progress_at = last_progress_at.replace(tzinfo=dt.UTC)
        no_progress_elapsed = (dt.datetime.now(dt.UTC) - last_progress_at.astimezone(dt.UTC)).total_seconds()
        if max_no_progress > 0 and no_progress_elapsed > max_no_progress and delivered < target_liters:
            await self.set_water_switches_state(switch_entity, "turn_off", master_switch=master_switch)
            await self.restore_appdaemon_manual_water_target(payload)
            await self.send_water_task_alert(
                payload,
                "failure_alert",
                (
                    f"Riego detenido por falta de progreso del contador. "
                    f"Entregados {delivered:.1f}/{target_liters:g} L."
                ),
            )
            raise RuntimeError(
                f"Riego detenido por falta de progreso del contador durante {no_progress_elapsed:.0f}s; "
                f"entregados {delivered:.1f}/{target_liters:g} L."
            )

        if delivered >= target_liters:
            await self.set_water_switches_state(switch_entity, "turn_off", master_switch=master_switch)
            await self.restore_appdaemon_manual_water_target(payload)
            await self.send_water_task_alert(
                payload,
                "completion_alert",
                f"Riego completado: {delivered:.1f}/{target_liters:g} L.",
            )
            return (
                f"Riego completado: contador {meter_entity} {current:.1f} L, "
                f"objetivo {target_value:.1f} L, entregados {delivered:.1f}/{target_liters:g} L. "
                f"{switch_entity} apagado."
            )

        if appdaemon_manual:
            actual_state, remaining = await self.appdaemon_water_state(payload)
            actual_folded = fold_accents(str(actual_state or "").strip().lower())
            if actual_folded in {"inactivo", "off", "idle"} and delivered < target_liters and elapsed > max(15.0, check_interval):
                await self.set_water_switches_state(switch_entity, "turn_off", master_switch=master_switch)
                await self.restore_appdaemon_manual_water_target(payload)
                await self.send_water_task_alert(
                    payload,
                    "failure_alert",
                    (
                        f"Riego detenido: AppDaemon ya no informa riego activo. "
                        f"Entregados {delivered:.1f}/{target_liters:g} L."
                    ),
                )
                raise RuntimeError(
                    f"Riego detenido antes del objetivo: AppDaemon={actual_state!r}, "
                    f"restante={remaining}, entregados {delivered:.1f}/{target_liters:g} L."
                )
            updated_instruction = "DETERMINISTIC_WATER_LITERS " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if self.current_task_id is not None:
                detail = f"Riego en curso: entregados {delivered:.1f}/{target_liters:g} L"
                if remaining is not None:
                    detail += f", restante AppDaemon {remaining:.1f} L"
                await self.reschedule_current_task(
                    {
                        "delay_seconds": check_interval,
                        "instruction": updated_instruction,
                        "reason": detail + ".",
                    }
                )
            status_extra = f" AppDaemon={actual_state}." if actual_state is not None else ""
            return (
                f"Riego en curso: entregados {delivered:.1f}/{target_liters:g} L. "
                f"AppDaemon mantiene {switch_entity}; revisaré de nuevo en {check_interval:g}s.{status_extra}"
            )

        switch_state = json.loads(
            await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": switch_entity})
        ).get("state")
        master_is_off = False
        if master_switch:
            master_state = json.loads(
                await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": master_switch})
            ).get("state")
            master_is_off = str(master_state).lower() != "on"
        if str(switch_state).lower() != "on" or master_is_off:
            await self.set_water_switches_state(switch_entity, "turn_on", master_switch=master_switch)
            await self.wait_water_power_verification(payload, switch_entity, master_switch)
        updated_instruction = "DETERMINISTIC_WATER_LITERS " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if self.current_task_id is not None:
            await self.reschedule_current_task(
                {
                    "delay_seconds": check_interval,
                    "instruction": updated_instruction,
                    "reason": (
                        f"Riego en curso: entregados {delivered:.1f}/{target_liters:g} L; "
                        f"contador {current:.1f}, objetivo {target_value:.1f}."
                    ),
                }
            )
        return (
            f"Riego en curso: entregados {delivered:.1f}/{target_liters:g} L. "
            f"{switch_entity} queda encendido; revisaré de nuevo en {check_interval:g}s."
        )

    async def try_execute_ac_power_control_task(self, instruction: str) -> str | None:
        prefix = "DETERMINISTIC_AC_POWER_CONTROL "
        if not instruction.strip().startswith(prefix):
            return None
        payload = json.loads(instruction.strip()[len(prefix) :])
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"turn_on", "turn_off"}:
            raise ValueError("DETERMINISTIC_AC_POWER_CONTROL requiere action turn_on o turn_off")
        entity_id = str(payload.get("entity_id") or "input_boolean.brokton_ac_dp1_switch").strip()
        expected_state = "on" if action == "turn_on" else "off"
        retry_wait_seconds = max(0.0, float(payload.get("retry_wait_seconds", 5) or 5))
        title = str(payload.get("alert_title") or "Fallo AC")
        action_label = "encendido" if action == "turn_on" else "apagado"

        async def call_action(service: str) -> dict[str, Any]:
            raw = await self.call_builtin_tool(
                "__builtin__:ha_call_service",
                {
                    "domain": "input_boolean",
                    "service": service,
                    "target": {"entity_id": entity_id},
                    "confirm": True,
                },
            )
            return json.loads(raw)

        async def verify_result(result: dict[str, Any]) -> tuple[bool, str]:
            state_raw = await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": entity_id})
            state = json.loads(state_raw)
            observed = str(state.get("state") or "")
            power_verification = result.get("power_verification") or {}
            state_ok = observed == expected_state
            power_ok = bool(power_verification.get("verified"))
            detail = (
                f"estado={observed}, "
                f"power_verification={json.dumps(power_verification, ensure_ascii=False)[:700]}"
            )
            return state_ok and power_ok, detail

        first_result = await call_action(action)
        ok, detail = await verify_result(first_result)
        if ok:
            return f"AC {action_label} verificado en primer intento: {detail}"

        if action == "turn_on":
            await call_action("turn_off")
            if retry_wait_seconds:
                await self.call_builtin_tool("__builtin__:wait_seconds", {"seconds": retry_wait_seconds})
            retry_result = await call_action("turn_on")
        else:
            if retry_wait_seconds:
                await self.call_builtin_tool("__builtin__:wait_seconds", {"seconds": retry_wait_seconds})
            retry_result = await call_action("turn_off")
        retry_ok, retry_detail = await verify_result(retry_result)
        if retry_ok:
            return f"AC {action_label} verificado tras un unico reintento: {retry_detail}"

        message = (
            f"No se pudo verificar el {action_label} del aire acondicionado Brokton tras un unico reintento. "
            f"Primer intento: {detail}. Reintento: {retry_detail}."
        )
        await self.call_builtin_tool(
            "__builtin__:ha_send_mobile_alert",
            {
                "title": title,
                "message": message,
            },
        )
        self.current_task_mobile_alert_sent = True
        raise RuntimeError(message)

    async def try_execute_tts_reminder_task(self, instruction: str) -> str | None:
        prefix = "DETERMINISTIC_TTS_REMINDER "
        if not instruction.strip().startswith(prefix):
            return None
        payload = json.loads(instruction.strip()[len(prefix) :])
        message = str(payload.get("message") or "").strip() or "hola"
        if bool(payload.get("include_time")):
            now = local_now()
            message = f"{message}, son las {now.strftime('%H:%M')}"
        entity_id = str(payload.get("media_player_entity_id") or "").strip()
        if not entity_id:
            players = json.loads(
                await self.call_builtin_tool(
                    "__builtin__:ha_get_tts_media_players",
                    {"query": str(payload.get("media_query") or ""), "limit": 10},
                )
            )
            candidates = players.get("candidates") if isinstance(players, dict) else players
            if isinstance(candidates, list) and candidates:
                entity_id = str(candidates[0].get("entity_id") or "")
        if not entity_id:
            raise RuntimeError("No hay media_player audible disponible para el recordatorio TTS.")
        await self.call_builtin_tool(
            "__builtin__:ha_call_service",
            {
                "domain": "tts",
                "service": "google_translate_say",
                "service_data": {
                    "cache": False,
                    "language": "es",
                    "entity_id": entity_id,
                    "message": message,
                },
                "confirm": True,
            },
        )
        return f"Mensaje TTS enviado a {entity_id}: {message}"

    async def try_execute_transition_condition_task(self, title: str, instruction: str) -> str | None:
        if "ha_count_state_transitions" not in instruction or "total_transitions" not in instruction:
            return None
        if "ha_call_service" not in instruction:
            return None

        source_match = re.search(r"entity_id\s*=\s*['\"](?P<entity>[^'\"]+)['\"]", instruction)
        start_match = re.search(r"start_time\s*=\s*(?P<start>[^,\s]+)", instruction)
        threshold_match = re.search(r"total_transitions\s*<\s*(?P<threshold>\d+)", instruction)
        if not source_match or not start_match or not threshold_match:
            return None

        from_match = re.search(r"from_state\s*=\s*['\"](?P<state>[^'\"]+)['\"]", instruction)
        to_match = re.search(r"to_state\s*=\s*['\"](?P<state>[^'\"]+)['\"]", instruction)
        domain_match = re.search(r"ha_call_service\s+domain\s*=\s*['\"](?P<domain>[^'\"]+)['\"]", instruction)
        service_match = re.search(r"ha_call_service\s+domain\s*=\s*['\"][^'\"]+['\"]\s*,\s*service\s*=\s*['\"](?P<service>[^'\"]+)['\"]", instruction)
        target_match = re.search(r"target\s*=\s*\{[^}]*['\"]entity_id['\"]\s*:\s*['\"](?P<entity>[^'\"]+)['\"]", instruction)
        delay_match = re.search(r"delay_minutes\s*=\s*(?P<delay>\d+)", instruction)
        if not domain_match or not service_match or not target_match:
            return None

        threshold = int(threshold_match.group("threshold"))
        delay_minutes = int(delay_match.group("delay")) if delay_match else 1
        count_args = {
            "start_time": start_match.group("start"),
            "entity_id": source_match.group("entity"),
            "from_state": from_match.group("state") if from_match else "off",
            "to_state": to_match.group("state") if to_match else "on",
        }
        count_text = await self.call_builtin_tool("__builtin__:ha_count_state_transitions", count_args)
        count_data = json.loads(count_text)
        total = int(count_data.get("total_transitions") or 0)
        source_entity = count_args["entity_id"]
        target_entity = target_match.group("entity")

        if total < threshold:
            await self.reschedule_current_task(
                {
                    "delay_minutes": delay_minutes,
                    "title": title,
                    "instruction": instruction,
                    "reason": f"Aun hay {total}/{threshold} transiciones {count_args['from_state']}->{count_args['to_state']} en {source_entity}",
                }
            )
            return f"Condición pendiente: {total}/{threshold} activaciones en {source_entity}. Reprogramada en {delay_minutes} minuto(s)."

        await self.call_builtin_tool(
            "__builtin__:ha_call_service",
            {
                "domain": domain_match.group("domain"),
                "service": service_match.group("service"),
                "target": {"entity_id": target_entity},
                "confirm": True,
            },
        )
        state_text = await self.call_builtin_tool("__builtin__:ha_get_state", {"entity_id": target_entity})
        state = "desconocido"
        with contextlib.suppress(Exception):
            state = str(json.loads(state_text).get("state") or state)
        return f"Condición cumplida: {total}/{threshold} activaciones en {source_entity}. Ejecutado {domain_match.group('domain')}.{service_match.group('service')} sobre {target_entity}; estado final {state}."

    def requires_execution_tool(self, instruction: str) -> bool:
        lowered = instruction.lower()
        explicit_tool_markers = (
            "ha_call_service",
            "ha_get_tts_media_players",
            "ha_send_mobile_alert",
            "ha_press_entity_interval",
            "ha_get_state",
            "ha_search_entities",
            "wait_seconds",
            "whatsapp_send_message",
            "whatsapp_list_contacts",
            "whatsapp_get_status",
            "whatsapp_get_recent_messages",
            "google_translate_say",
            "domain='tts'",
            'domain="tts"',
            "service='google_translate_say'",
            'service="google_translate_say"',
        )
        if any(marker in lowered for marker in explicit_tool_markers):
            return True
        action_words = (
            "enciende",
            "encender",
            "apaga",
            "apagar",
            "activa",
            "activar",
            "desactiva",
            "desactivar",
            "abre",
            "abrir",
            "cierra",
            "cerrar",
            "pon ",
            "poner ",
            "sube",
            "subir",
            "baja",
            "bajar",
            "cambia",
            "cambiar",
            "envia",
            "envía",
            "enviar",
            "notifica",
            "notificar",
            "alerta",
            "avisar",
            "avisa",
            "toca",
            "tocar",
            "suena",
            "sonar",
            "reproduce",
            "reproducir",
            "usa",
            "usar",
            "habla",
            "hablar",
            "di ",
            "dí ",
            "decir",
            "anuncia",
            "anunciar",
        )
        homeassistant_words = (
            "switch",
            "interruptor",
            "luz",
            "luces",
            "rele",
            "relay",
            "sensor",
            "media_player",
            "altavoz",
            "tts",
            "voz",
            "hablado",
            "hablada",
            "persiana",
            "termostato",
            "sirena",
            "alarma",
            "home assistant",
            "salon",
            "salón",
            "comedor",
            "terreno",
            "nspanel",
            "rtttl",
            "esphome",
            "tono",
            "melodia",
            "melodía",
            "nokia",
            "timbre",
        )
        return any(word in lowered for word in action_words) and any(word in lowered for word in homeassistant_words)

    def simple_reminder_message(self, instruction: str) -> str | None:
        text = instruction.strip()
        lowered = text.lower()
        if re.match(r"^\s*(di|dí|habla|anuncia|avisa por voz|reproduce|avisa|avísame|avisame|alerta|llama|llámame|llamame)\b", lowered):
            return None
        prefixes = (
            "decir ",
            "dime ",
            "avisame ",
            "avísame ",
            "recordarme ",
            "recuerdame ",
            "recuérdame ",
        )
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return text[len(prefix) :].strip() or text
        match = re.search(r"\bdecir\s+(.+)$", text, flags=re.IGNORECASE)
        if match and not self.requires_execution_tool(lowered):
            return match.group(1).strip()
        if not self.requires_execution_tool(lowered):
            return text
        return None

    async def _run_task_unlocked(
        self,
        user_text: str,
        *,
        require_tool: bool = False,
        require_action: bool = False,
        allow_mobile_alert: bool = False,
    ) -> str:
        working_messages = [
            {"role": "system", "content": self._system_prompt(user_text)},
            {"role": "user", "content": user_text},
        ]
        tool_calls_executed = 0
        action_calls_executed = 0
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._chat(working_messages, tools=True, task="scheduled_task")
            msg = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                if require_tool and tool_calls_executed == 0:
                    raise RuntimeError(
                        "La tarea requería herramientas, pero el modelo no llamó a ninguna."
                    )
                if require_action and action_calls_executed == 0:
                    raise RuntimeError(
                        "La tarea requería una acción de Home Assistant, pero el modelo solo consultó datos o no llamó a ninguna acción."
                    )
                return msg.content or "Tarea ejecutada sin respuesta del modelo."

            working_messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else call
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                public_name = call.function.name
                real_name = self.tool_name_map.get(public_name, public_name)
                try:
                    tool_args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}

                if (
                    real_name == "__builtin__:ha_send_mobile_alert"
                    and not allow_mobile_alert
                ):
                    working_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": compact_json(
                                {
                                    "sent": False,
                                    "blocked": True,
                                    "reason": (
                                        "La tarea no pide explicitamente una notificacion "
                                        "al movil o telefono."
                                    ),
                                }
                            ),
                        }
                    )
                    continue

                tool_calls_executed += 1

                if self.is_execution_action_tool(real_name):
                    action_calls_executed += 1

                if real_name.startswith("__builtin__:"):
                    print(f"\n[interno tarea] {real_name.removeprefix('__builtin__:')} {compact_json(tool_args, 800)}", flush=True)
                    content_text = await self.call_builtin_tool(real_name, tool_args)
                else:
                    print(f"\n[MCP tarea] {real_name} {compact_json(tool_args, 800)}", flush=True)
                    result = await self.session.call_tool(real_name, tool_args)
                    content_text = tool_result_to_text(result) or "(sin contenido)"
                working_messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content_text}
                )
                if real_name == "__builtin__:reschedule_current_task":
                    with contextlib.suppress(Exception):
                        if json.loads(content_text).get("rescheduled"):
                            return "Tarea reprogramada porque la condición todavía no se cumple."
        raise RuntimeError("La tarea superó el límite interno de herramientas.")

    @staticmethod
    def is_execution_action_tool(real_name: str) -> bool:
        action_names = {
            "__builtin__:ha_call_service",
            "__builtin__:ha_press_entity_interval",
            "__builtin__:ha_send_mobile_alert",
            "__builtin__:whatsapp_send_message",
            "HassTurnOn",
            "HassTurnOff",
            "HassToggle",
            "HassCallService",
            "HassSetState",
            "HassSetPosition",
            "HassLightSet",
            "HassClimateSetTemperature",
            "HassClimateSetHvacMode",
            "HassMediaPlayerPlayMedia",
            "HassMediaPlayerPause",
            "HassMediaPlayerStop",
            "HassMediaPlayerVolumeSet",
            "HassVacuumStart",
            "HassVacuumReturnToBase",
            "HassLockLock",
            "HassLockUnlock",
            "HassCoverOpen",
            "HassCoverClose",
            "HassCoverStop",
        }
        return real_name in action_names

    async def call_builtin_tool(self, real_name: str, args: dict[str, Any]) -> str:
        if real_name == "__builtin__:create_event_listener":
            return await self.create_event_listener(args)
        if real_name == "__builtin__:list_event_listeners":
            return await self.list_event_listeners(args)
        if real_name == "__builtin__:cancel_event_listener":
            return await self.cancel_event_listener(args)
        if real_name == "__builtin__:schedule_automation":
            return await self.schedule_automation(args)
        if real_name == "__builtin__:schedule_task":
            return await self.schedule_task(args)
        if real_name == "__builtin__:reschedule_current_task":
            return await self.reschedule_current_task(args)
        if real_name == "__builtin__:list_scheduled_tasks":
            return await self.list_scheduled_tasks(args)
        if real_name == "__builtin__:edit_scheduled_task":
            return await self.edit_scheduled_task(args)
        if real_name == "__builtin__:cancel_scheduled_task":
            return await self.cancel_scheduled_task(args)
        if real_name == "__builtin__:wait_seconds":
            max_wait = max(0.1, min(60.0, float(TASK_TIMEOUT_SECONDS - 5)))
            seconds = max(0.1, min(float(args.get("seconds", 1) or 1), max_wait))
            await asyncio.sleep(seconds)
            return compact_json({"waited_seconds": seconds})
        if real_name == "__builtin__:whatsapp_send_message":
            if self.whatsapp_bridge is None:
                raise RuntimeError("el canal WhatsApp no está activo")
            message = str(args.get("message") or "").strip()
            recipient = str(args.get("to") or "last_sender").strip()
            await self.whatsapp_bridge.send_message(message, recipient)
            return compact_json(
                {
                    "sent": True,
                    "to": "last_sender" if recipient == "last_sender" else recipient,
                }
            )
        if real_name == "__builtin__:whatsapp_list_contacts":
            if self.whatsapp_bridge is None:
                raise RuntimeError("el canal WhatsApp no está activo")
            contacts = self.whatsapp_bridge.list_contacts(
                query=str(args.get("query") or ""),
                limit=int(args.get("limit") or 50),
            )
            return compact_json({"count": len(contacts), "contacts": contacts})
        if real_name == "__builtin__:whatsapp_get_status":
            if self.whatsapp_bridge is None:
                raise RuntimeError("el canal WhatsApp no está activo")
            return compact_json(self.whatsapp_bridge.channel_status())
        if real_name == "__builtin__:whatsapp_get_recent_messages":
            if self.whatsapp_bridge is None:
                raise RuntimeError("el canal WhatsApp no está activo")
            messages = self.whatsapp_bridge.recent_messages(
                limit=int(args.get("limit") or 20),
                direction=str(args.get("direction") or "all"),
            )
            return compact_json({"count": len(messages), "messages": messages})
        indexed_name = real_name.removeprefix("__builtin__:")
        if indexed_name in builtin_tool_names(include_homeassistant=self.has_homeassistant_rest):
            result = await call_indexed_builtin_tool(self, indexed_name, args)
            if indexed_name == "ha_call_service":
                self.remember_action_verifications(args)
            if indexed_name == "ha_send_mobile_alert":
                if self.current_task_id is not None:
                    self.current_task_mobile_alert_sent = True
                else:
                    self.request_mobile_alert_sent = True
            return result
        raise ValueError(f"Herramienta interna desconocida: {real_name}")

    def remember_action_verifications(self, args: dict[str, Any]) -> None:
        """Queue expected HA states after a successful service call."""

        service = str(args.get("service") or "").strip().lower()
        domain = str(args.get("domain") or "").strip().lower()
        expected_by_service = {
            "turn_on": "on",
            "turn_off": "off",
            "lock": "locked",
            "unlock": "unlocked",
            "open_cover": "open",
            "close_cover": "closed",
            "open": "open",
            "close": "closed",
        }
        expected = expected_by_service.get(service)
        service_data = args.get("service_data") if isinstance(args.get("service_data"), dict) else {}
        if service == "select_option":
            expected = str(service_data.get("option") or "") or None
        elif service == "set_hvac_mode":
            expected = str(service_data.get("hvac_mode") or "") or None
        elif service == "set_value":
            value = service_data.get("value")
            expected = str(value) if value is not None else None
        if expected is None or domain in {"notify", "tts", "script", "automation"}:
            return

        target = args.get("target")
        entity_ids: Any = target.get("entity_id") if isinstance(target, dict) else args.get("entity_id")
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if not isinstance(entity_ids, list):
            return
        try:
            pending = json.loads(self.memory.get_setting("agents.pending_verifications", "[]") or "[]")
        except json.JSONDecodeError:
            pending = []
        pending = [item for item in pending if isinstance(item, dict)]
        for entity in entity_ids:
            entity_id = str(entity or "").strip()
            if "." not in entity_id:
                continue
            pending = [item for item in pending if item.get("entity_id") != entity_id]
            pending.append(
                {
                    "entity_id": entity_id,
                    "expected_state": expected,
                    "domain": domain,
                    "service": service,
                    "requested_at": utc_now(),
                }
            )
        self.memory.set_setting("agents.pending_verifications", json.dumps(pending[-100:], ensure_ascii=False))

    async def create_event_listener(self, args: dict[str, Any]) -> str:
        listener_id = create_subscription(
            self.memory.conn,
            title=str(args.get("title") or ""),
            instruction=str(args.get("instruction") or ""),
            event_type=str(args.get("event_type") or "state_changed"),
            entity_id=str(args.get("entity_id") or "") or None,
            from_state=str(args["from_state"]) if args.get("from_state") is not None else None,
            to_state=str(args["to_state"]) if args.get("to_state") is not None else None,
            attribute=str(args.get("attribute") or "") or None,
            operator=str(args.get("operator") or "") or None,
            expected_value=args.get("expected_value"),
            event_data=args.get("event_data"),
            cooldown_seconds=int(args.get("cooldown_seconds") or 0),
            once_only=bool(args.get("once_only", False)),
            priority=int(args.get("priority") or 50),
            cancellation_key=normalize_cancellation_key(
                str(args.get("cancellation_key") or "")
            )
            or None,
        )
        return compact_json(
            {
                "created": True,
                "listener_id": listener_id,
                "event_type": str(args.get("event_type") or "state_changed"),
                "entity_id": str(args.get("entity_id") or "") or None,
                "title": str(args.get("title") or ""),
            }
        )

    def try_create_simple_event_listener(self, user_text: str) -> str | None:
        compiled = compile_state_action_listener(
            user_text,
            resolve_entity=self.resolve_site_alias,
        )
        if compiled is None:
            return None
        listener_id = create_subscription(
            self.memory.conn,
            title=compiled.title,
            instruction=encode_plan(compiled.plan),
            entity_id=compiled.entity_id,
            from_state=compiled.from_state,
            to_state=compiled.to_state,
            priority=70,
        )
        return (
            f"He creado la escucha #{listener_id}: {compiled.summary}. "
            "La acción queda guardada como un plan estructurado y determinista."
        )

    def try_cancel_event_listener_semantic(self, user_text: str) -> str | None:
        if not is_listener_cancel_request(user_text):
            return None
        rows = list_subscriptions(self.memory.conn, enabled_only=True, limit=200)
        if not rows:
            return "No hay escuchas activas que cancelar."

        def entity_context(entity_id: str) -> str:
            role = self.site_profile.role_for_entity(entity_id)
            if role is None:
                return entity_id
            role_name, binding = role
            aliases = " ".join(str(item) for item in binding.get("aliases") or [])
            return " ".join(
                (
                    entity_id,
                    role_name,
                    str(binding.get("label") or ""),
                    aliases,
                )
            )

        candidates: list[tuple[int, str]] = []
        for row in rows:
            parts = [
                str(row["title"]),
                entity_context(str(row["entity_id"] or "")),
                str(row["from_state"] or ""),
                str(row["to_state"] or ""),
            ]
            if str(row["to_state"] or "") == "on":
                parts.append("enciende encender activa activar")
            elif str(row["to_state"] or "") == "off":
                parts.append("apaga apagar desactiva desactivar")
            plan = decode_plan(str(row["instruction"]))
            if plan:
                for step in plan["steps"]:
                    if step["type"] != "service":
                        continue
                    parts.extend((str(step["domain"]), str(step["service"])))
                    service = str(step["service"])
                    if service == "turn_on":
                        parts.append("enciende encender activa activar")
                    elif service == "turn_off":
                        parts.append("apaga apagar desactiva desactivar")
                    targets = step.get("target") or {}
                    entity_ids = targets.get("entity_id")
                    if isinstance(entity_ids, str):
                        entity_ids = [entity_ids]
                    for entity_id in entity_ids or []:
                        parts.append(entity_context(str(entity_id)))
            candidates.append((int(row["id"]), " ".join(parts)))

        listener_id = select_listener_candidate(user_text, candidates)
        if listener_id is None:
            choices = ", ".join(
                f"#{int(row['id'])} {row['title']}" for row in rows[:10]
            )
            return (
                "No pude identificar una única escucha. "
                f"Escuchas activas: {choices}."
            )
        row = next(row for row in rows if int(row["id"]) == listener_id)
        if not set_subscription_enabled(self.memory.conn, listener_id, False):
            return f"La escucha #{listener_id} ya no estaba activa."
        return f"He cancelado la escucha #{listener_id}: {row['title']}."

    async def list_event_listeners(self, args: dict[str, Any]) -> str:
        include_disabled = bool(args.get("include_disabled", False))
        rows = list_subscriptions(
            self.memory.conn,
            enabled_only=not include_disabled,
            limit=max(1, min(int(args.get("limit") or 50), 200)),
        )
        return compact_json(
            {
                "listeners": [
                    {
                        "id": int(row["id"]),
                        "enabled": bool(row["enabled"]),
                        "title": row["title"],
                        "event_type": row["event_type"],
                        "entity_id": row["entity_id"],
                        "from_state": row["from_state"],
                        "to_state": row["to_state"],
                        "attribute": row["attribute"],
                        "operator": row["operator"],
                        "cooldown_seconds": int(row["cooldown_seconds"] or 0),
                        "once_only": bool(row["once_only"]),
                        "trigger_count": int(row["trigger_count"] or 0),
                        "last_triggered_at": row["last_triggered_at"],
                    }
                    for row in rows
                ]
            }
        )

    async def cancel_event_listener(self, args: dict[str, Any]) -> str:
        listener_id = int(args.get("listener_id") or 0)
        if listener_id < 1:
            raise ValueError("listener_id es obligatorio")
        if not set_subscription_enabled(self.memory.conn, listener_id, False):
            raise ValueError(f"Escucha #{listener_id} no encontrada")
        return compact_json({"cancelled": True, "listener_id": listener_id})

    async def send_scheduled_completion_alert(
        self,
        *,
        title: str,
        instruction: str,
        result: str,
    ) -> str:
        if self.current_task_mobile_alert_sent:
            return result
        if explicit_whatsapp_send_intent(instruction):
            return result
        plan = decode_plan(instruction) or decode_legacy_plan(instruction)
        notification = dict(plan.get("completion_notification") or {}) if plan else {}
        if notification and not notification.get("enabled", True):
            return result
        if notification_preference_declined(instruction):
            return result
        if not notification and not mobile_notification_explicitly_requested(instruction):
            return result
        notification.pop("enabled", None)
        notification.setdefault("title", "Codexon")
        notification.setdefault("message", f"Tarea completada: {title}. {result[:500]}")
        try:
            await self.call_builtin_tool("__builtin__:ha_send_mobile_alert", notification)
            self.current_task_mobile_alert_sent = True
            return f"{result} Aviso móvil de finalización enviado."
        except Exception as exc:  # La tarea ya termino; no se repite por un fallo de notificacion.
            self.memory.add_event(
                level="error",
                message=f"Fallo enviando aviso de finalizacion de '{title}': {exc}",
            )
            return f"{result} La tarea terminó, pero falló el aviso móvil: {exc}"

    async def schedule_automation(self, args: dict[str, Any]) -> str:
        run_at = str(args.get("run_at", "")).strip()
        title = str(args.get("title", "")).strip()
        plan = validate_plan(args.get("plan") or {})
        if not run_at or not title:
            raise ValueError("run_at, title y plan son obligatorios")
        interval_seconds = validate_task_interval(args.get("interval_seconds"))
        cancellation_key = str(args.get("cancellation_key") or "").strip() or None
        priority = max(0, min(int(args.get("priority", 50) or 50), 100))
        task_id = self.memory.add_task(
            run_at=run_at,
            title=title,
            instruction=encode_plan(plan),
            interval_seconds=interval_seconds,
            cancellation_key=cancellation_key,
            priority=priority,
        )
        rows = self.memory.list_tasks(include_done=True, limit=200)
        created = next((row for row in rows if row["id"] == task_id), None)
        return compact_json(
            {
                "created": True,
                "task_id": task_id,
                "run_at_utc": created["run_at"] if created else parse_datetime_to_utc(run_at),
                "title": title,
                "plan": plan,
                "interval_seconds": interval_seconds,
                "cancellation_key": normalize_cancellation_key(cancellation_key or ""),
            }
        )

    async def schedule_task(self, args: dict[str, Any]) -> str:
        run_at = str(args.get("run_at", "")).strip()
        title = str(args.get("title", "")).strip()
        instruction = str(args.get("instruction", "")).strip()
        if not run_at or not title or not instruction:
            raise ValueError("run_at, title e instruction son obligatorios")
        interval_seconds = validate_task_interval(args.get("interval_seconds"))
        cancellation_key = str(args.get("cancellation_key") or "").strip() or None
        max_attempts = max(1, min(int(args.get("max_attempts", DEFAULT_TASK_MAX_ATTEMPTS) or DEFAULT_TASK_MAX_ATTEMPTS), 20))
        retry_backoff_seconds = max(
            1,
            min(
                int(args.get("retry_backoff_seconds", DEFAULT_TASK_RETRY_BACKOFF_SECONDS) or DEFAULT_TASK_RETRY_BACKOFF_SECONDS),
                86400,
            ),
        )
        task_id = self.memory.add_task(
            run_at=run_at,
            title=title,
            instruction=instruction,
            interval_seconds=interval_seconds,
            cancellation_key=cancellation_key,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        task = self.memory.list_tasks(include_done=True, limit=200)
        created = next((row for row in task if row["id"] == task_id), None)
        return compact_json(
            {
                "created": True,
                "task_id": task_id,
                "run_at_utc": created["run_at"] if created else parse_datetime_to_utc(run_at),
                "title": title,
                "instruction": instruction,
                "interval_seconds": interval_seconds,
                "cancellation_key": normalize_cancellation_key(cancellation_key or ""),
                "max_attempts": max_attempts,
                "retry_backoff_seconds": retry_backoff_seconds,
            }
        )

    async def reschedule_current_task(self, args: dict[str, Any]) -> str:
        if self.current_task_id is None or self.current_task_run_id is None:
            raise ValueError("reschedule_current_task solo puede usarse mientras se ejecuta una tarea programada")
        run_at = str(args.get("run_at", "") or "").strip()
        if not run_at:
            if args.get("delay_seconds") is not None:
                delay_seconds = max(1.0, min(float(args.get("delay_seconds") or 1), 86400.0))
                delta = dt.timedelta(seconds=delay_seconds)
            else:
                delay_minutes = max(1, min(int(args.get("delay_minutes", 5) or 5), 1440))
                delta = dt.timedelta(minutes=delay_minutes)
            run_at = (dt.datetime.now(ZoneInfo(DEFAULT_TIMEZONE)) + delta).isoformat(timespec="seconds")
        updated = self.memory.reschedule_running_task(
            task_id=self.current_task_id,
            worker_id=self.worker_id,
            run_id=self.current_task_run_id,
            run_at=run_at,
            title=str(args["title"]).strip() if args.get("title") else None,
            instruction=str(args["instruction"]).strip() if args.get("instruction") else None,
        )
        if updated:
            self.rescheduled_task_ids.add(self.current_task_id)
        return compact_json(
            {
                "rescheduled": updated,
                "task_id": self.current_task_id,
                "run_at_utc": parse_datetime_to_utc(run_at),
                "reason": str(args.get("reason", "") or "").strip() or None,
            }
        )

    async def list_scheduled_tasks(self, args: dict[str, Any]) -> str:
        include_done = bool(args.get("include_done", False))
        limit = int(args.get("limit", 30) or 30)
        rows = self.memory.list_tasks(include_done=include_done, limit=max(1, min(limit, 100)))
        return format_task_list(rows, include_done=include_done)

    async def edit_scheduled_task(self, args: dict[str, Any]) -> str:
        task_id = int(args.get("task_id"))
        updated = self.memory.update_task(
            task_id=task_id,
            run_at=str(args["run_at"]).strip() if args.get("run_at") else None,
            title=str(args["title"]).strip() if args.get("title") else None,
            instruction=str(args["instruction"]).strip() if args.get("instruction") else None,
            status=str(args["status"]).strip() if args.get("status") else None,
        )
        return compact_json({"updated": updated, "task_id": task_id})

    async def cancel_scheduled_task(self, args: dict[str, Any]) -> str:
        task_id = int(args.get("task_id"))
        cancelled = self.memory.cancel_task(task_id)
        return compact_json({"cancelled": cancelled, "task_id": task_id})

    async def learn_from_turn(self, user_text: str, answer: str) -> None:
        prompt = f"""
Extrae memorias duraderas útiles de esta interacción.
Devuelve JSON estricto con esta forma:
{{"memories":[{{"kind":"preferencia|hecho|patron|instruccion","topic":"...","content":"...","confidence":0.0}}]}}

Guarda solo información que pueda ser útil en futuras conversaciones o para interpretar sensores.
No guardes saludos, contenido temporal trivial ni estados instantáneos salvo que indiquen un patrón.

Usuario: {user_text}
Asistente: {answer}
""".strip()
        data: dict[str, Any] = {}
        last_error: Exception | None = None
        last_raw = ""
        interactive_model = self.memory.get_setting("interactive_model") or None
        free_interactive_model = False
        if interactive_model:
            prices = self.router.price_tuple(interactive_model)
            free_interactive_model = prices == (0.0, 0.0)
        memory_models: tuple[str | None, ...] = (
            (interactive_model,)
            if free_interactive_model
            else (None, "openai/gpt-4.1-nano")
        )
        for preferred_model in memory_models:
            try:
                response = await self._chat(
                    [
                        {"role": "system", "content": "Eres un extractor de memoria. Responde solo JSON válido, sin markdown."},
                        {"role": "user", "content": prompt},
                    ],
                    tools=False,
                    task="memory_extraction",
                    preferred_model=preferred_model,
                )
                last_raw = response.choices[0].message.content or ""
                data = extract_json_object(last_raw)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        if last_error is not None and not data:
            self.memory.add_event(level="warn", message=f"No se pudo extraer memoria: {last_error}", raw=last_raw)
            return

        for item in data.get("memories", []):
            content = str(item.get("content", "")).strip()
            topic = str(item.get("topic", "")).strip() or "general"
            kind = str(item.get("kind", "hecho")).strip() or "hecho"
            if len(content) < 12:
                continue
            confidence = float(item.get("confidence", 0.7))
            self.memory.add_memory(
                kind=kind,
                topic=topic,
                content=content,
                confidence=max(0.0, min(1.0, confidence)),
                source="conversation",
            )

    async def observe_sensors(self, sensor_prompt: str) -> None:
        prompt = f"""
Haz una observación de sensores de Home Assistant usando MCP.

Instrucciones del usuario/configuración:
{sensor_prompt}

Devuelve una respuesta breve con:
- estados relevantes
- cambios o anomalías
- riesgo bajo/medio/alto
- una recomendación si procede
""".strip()
        answer = await self.ask(prompt)
        self.memory.add_observation(source="sensor-loop", summary=answer, raw=None)


async def sensor_loop(agent: CodexonAgent, poll_seconds: int, sensor_prompt: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await agent.observe_sensors(sensor_prompt)
        except Exception as exc:  # noqa: BLE001
            agent.memory.add_event(level="error", message=f"Fallo en observación de sensores: {exc}")
            print(f"\n[observador] error: {exc}")

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


def task_status_writer(*, reprompt: bool = False):
    def write(message: str) -> None:
        print(message, flush=True)
        if reprompt and sys.stdout.isatty():
            print("\nCodexon> ", end="", flush=True)

    return write


async def await_with_terminal_heartbeat(
    awaitable,
    *,
    label: str = "procesando",
    interval_seconds: int = 10,
) -> Any:
    task = asyncio.create_task(awaitable)
    started = time.monotonic()
    interval_seconds = max(1, interval_seconds)
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=interval_seconds)
        except asyncio.TimeoutError:
            elapsed = int(time.monotonic() - started)
            print(f"[{label}] {elapsed}s transcurridos; sigo trabajando...", flush=True)
        except BaseException:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise


async def task_loop(agent: CodexonAgent, stop: asyncio.Event, *, reprompt: bool = False) -> None:
    await run_task_loop(
        agent,
        stop,
        task_timeout_seconds=TASK_TIMEOUT_SECONDS,
        runtime_log=runtime_log,
        exception_summary=exception_summary,
        utc_now=utc_now,
        status_writer=task_status_writer(reprompt=reprompt),
    )


def history_file_path() -> Path:
    return Path(DEFAULT_HISTORY_FILE).expanduser()


def last_interaction_path() -> Path:
    return Path(DEFAULT_LAST_INTERACTION_FILE).expanduser()


def save_last_interaction(record: dict[str, Any]) -> None:
    path = last_interaction_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_consultalo_request(user_text: str, command: str, rest: str) -> str | None:
    if command in {"/consultalo", "/consulta", "/teach"}:
        return rest.strip()
    lowered = user_text.casefold()
    if lowered in {"consultalo", "consúltalo", "consulta esto"}:
        return ""
    for prefix in ("consultalo:", "consúltalo:", "consulta esto:", "consulta esto ", "consultalo ", "consúltalo "):
        if lowered.startswith(prefix):
            return user_text[len(prefix) :].strip()
    return None


def create_teaching_note(instruction: str) -> None:
    instruction = instruction.strip()
    if not instruction:
        instruction = "Revisa la ultima interaccion y corrige Codexon para que responda mejor."
    teach_bin = "/usr/local/bin/codexon-teach" if Path("/usr/local/bin/codexon-teach").exists() else "codexon-teach"
    cmd = [
        teach_bin,
        "--severity",
        "normal",
        "--error-class",
        "USER_CONSULTALO",
        "--observed",
        instruction,
        instruction,
    ]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        print("No encuentro codexon-teach. Ejecuta desde el entorno Codexon o revisa /usr/local/bin.")
        return
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode != 0:
        print(f"codexon-teach termino con codigo {result.returncode}.")


def codex_maintenance_prompt(instruction: str) -> str:
    return textwrap.dedent(
        f"""
        Trabaja como mantenedor de Codexon a partir de esta petición del usuario:

        {instruction.strip()}

        Revisa primero /data/codexon/teach/latest.md y el contexto vivo disponible.
        Inspecciona el comportamiento existente antes de cambiarlo. Si la petición es ambigua,
        pregunta al usuario dentro de esta sesión de Codex. Si requiere código, modifica la fuente
        canónica del workspace cuando exista y sincroniza la copia activa /data/codexon/app solo
        después de verificarla. Conserva datos sensibles, memoria y sesiones. Ejecuta pruebas
        proporcionales al cambio, no publiques en GitHub salvo petición explícita y explica el resultado.
        """
    ).strip()


def run_codex_maintenance(instruction: str) -> bool:
    instruction = instruction.strip()
    if not instruction:
        print("Uso: /codex <qué quieres enseñar, corregir o modificar>")
        return False
    create_teaching_note(instruction)
    workspace = Path(os.getenv("CODEXON_WORKSPACE") or os.getenv("WORKSPACE") or "/ha_config")
    if not workspace.exists():
        workspace = Path("/data/codexon/app")
    command = [
        "codex",
        "--model",
        os.getenv("CODEX_MODEL", "gpt-5.3-codex"),
        codex_maintenance_prompt(instruction),
    ]
    print("\nAbriendo Codex de mantenimiento. Al salir volverás a codexon-chat.\n", flush=True)
    try:
        result = subprocess.run(command, cwd=workspace, check=False)
    except FileNotFoundError:
        print("No encuentro el ejecutable codex. Revisa la instalación o autenticación del add-on.")
        return False
    if result.returncode != 0:
        print(f"Codex terminó con código {result.returncode}.")
        return False
    print("\nSesión Codex terminada; vuelves a codexon-chat.")
    return True


def setup_console_history() -> None:
    if readline is None:
        runtime_log("warn", "console", "readline no disponible; historial de flechas desactivado")
        return
    history_path = history_file_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        readline.read_history_file(str(history_path))
    readline.set_history_length(DEFAULT_HISTORY_LIMIT)
    for binding in (
        "tab: complete",
        '"\\e[A": history-search-backward',
        '"\\e[B": history-search-forward',
    ):
        with contextlib.suppress(Exception):
            readline.parse_and_bind(binding)
    atexit.register(save_console_history)


def save_console_history() -> None:
    if readline is None:
        return
    history_path = history_file_path()
    with contextlib.suppress(Exception):
        history_path.parent.mkdir(parents=True, exist_ok=True)
        readline.set_history_length(DEFAULT_HISTORY_LIMIT)
        readline.write_history_file(str(history_path))
        history_path.chmod(0o600)


def remember_console_line(line: str) -> None:
    if readline is None:
        return
    text = line.strip()
    if not text:
        return
    current_len = readline.get_current_history_length()
    previous = readline.get_history_item(current_len) if current_len else None
    if previous != text:
        readline.add_history(text)
    save_console_history()


async def async_input(prompt: str) -> str:
    line = await asyncio.to_thread(input, prompt)
    remember_console_line(line)
    return line


def print_rows(title: str, rows: list[sqlite3.Row], empty: str) -> None:
    print(f"\n{title}")
    if not rows:
        print(empty)
        return
    for row in rows:
        if "content" in row.keys():
            print(f"- #{row['id']} {row['topic']}: {row['content']}")
        else:
            print(f"- #{row['id']} {row['created_at']} {row['source']}: {row['summary']}")


def print_tasks(rows: list[sqlite3.Row]) -> None:
    print("\n" + format_task_list(rows))


def print_usage_summary(summary: dict[str, Any]) -> None:
    total = summary["total"]
    print("\nCoste LLM")
    print(
        "Total: "
        f"{total['calls']} llamadas, "
        f"{total['prompt_tokens']} entrada, "
        f"{total['completion_tokens']} salida, "
        f"{total['total_tokens']} tokens, "
        f"${total['cost']:.6f} estimado"
    )
    print("Recientes:")
    for row in summary["recent"]:
        cost = row["estimated_cost_usd"]
        cost_text = f"${cost:.6f}" if cost is not None else "sin estimar"
        duration = f"{row['duration_ms']}ms" if row.get("duration_ms") is not None else "sin duracion"
        provider = row.get("provider") or "proveedor no informado"
        print(
            f"- {row['created_at']} {row['context']} {row['model']}: "
            f"{row['prompt_tokens']}/{row['completion_tokens']} tokens, {cost_text}, {duration}, {provider}"
        )
        if row.get("router_reason"):
            print(f"  motivo: {row['router_reason']}")


def mask_secret(value: str | None, *, visible: int = 4) -> str:
    if not value:
        return "no configurado"
    if len(value) <= visible * 2:
        return "configurado"
    return f"{value[:visible]}...{value[-visible:]}"


def write_env_values(updates: dict[str, str], env_path: Path = Path(".env")) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")


def model_metadata_text(router: ModelRouter, model_id: str) -> str:
    meta = router.model_catalog.get(model_id) or {}
    if not meta:
        return "metadatos no disponibles"
    context_length = meta.get("context_length")
    context_text = f"{int(context_length):,}".replace(",", ".") if context_length else "?"
    tools_text = "si" if meta.get("supports_tools") else "no"
    input_price = float(meta.get("input_price_per_million") or 0)
    output_price = float(meta.get("output_price_per_million") or 0)
    return (
        f"tools={tools_text}, contexto={context_text}, "
        f"entrada=${input_price:.3f}/M, salida=${output_price:.3f}/M"
    )


def format_model_menu(
    router: ModelRouter,
    selected_model: str | None,
    *,
    page: int = 1,
    page_size: int = 50,
    configured_only: bool = False,
) -> str:
    models = router.configured_models() if configured_only else router.selectable_models()
    total_pages = max(1, (len(models) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    visible_models = models[start : start + page_size]
    current = selected_model or f"automatico ({router.config.get('routes', {}).get('homeassistant', {}).get('model') or router.config.get('default')})"
    title = "Modelos configurados" if configured_only else f"Catalogo oficial OpenRouter compatible con chat/tools: pagina {page}/{total_pages}, {len(models)} modelos"
    lines = [f"Modelo interactivo actual: {current}", "", f"{title}:"]
    for index, model_id in enumerate(visible_models, start=start + 1):
        marker = " *" if model_id == selected_model else ""
        lines.append(f"{index}. {model_id}{marker} [{model_metadata_text(router, model_id)}]")
    if not configured_only and models:
        lines.append(f"\nMostrando {start + 1}-{start + len(visible_models)} de {len(models)} modelos.")
    lines.extend(
        [
            "",
            "Uso:",
            "/model <numero|id>       Seleccionar modelo",
            "/model pagina <n>        Ver otra pagina del catalogo oficial",
            "/model configurados      Ver solo los modelos del router",
            "/model auto              Volver al router automatico",
            "/model buscar <texto>    Buscar en OpenRouter",
            "/model actualizar        Recargar catalogo oficial",
            "/model pregunta <texto>  Guardar pregunta de prueba",
            "/model probar            Repetir la pregunta guardada",
        ]
    )
    return "\n".join(lines)


async def print_model_catalog_pages(
    router: ModelRouter,
    selected_model: str | None,
    *,
    start_page: int = 1,
    page_size: int = 50,
) -> None:
    models = router.selectable_models()
    total_pages = max(1, (len(models) + page_size - 1) // page_size)
    page = max(1, min(start_page, total_pages))
    while True:
        print(format_model_menu(router, selected_model, page=page, page_size=page_size))
        if page >= total_pages:
            return
        response = (await async_input("\n¿Siguiente? [s/N] ")).strip().lower()
        if fold_accents(response) not in {"s", "si", "y", "yes", "siguiente"}:
            return
        page += 1


def format_model_search_results(router: ModelRouter, query: str, matches: list[str]) -> str:
    indexes = {model_id: index for index, model_id in enumerate(router.selectable_models(), start=1)}
    lines = [f"Modelos encontrados para '{query}':"]
    for model_id in matches:
        lines.append(f"{indexes.get(model_id, '?')}. {model_id} [{model_metadata_text(router, model_id)}]")
    lines.append("Selecciona uno con /model <numero|id>.")
    return "\n".join(lines)


def resolve_model_reference(router: ModelRouter, reference: str) -> str | None:
    models = router.selectable_models()
    if reference.isdigit():
        index = int(reference) - 1
        return models[index] if 0 <= index < len(models) else None
    if reference in models:
        return reference
    return None


def model_used_text(agent: CodexonAgent) -> str:
    if agent.last_interactive_model_used:
        fallback = (
            f"; solicitado {agent.preferred_model}"
            if agent.preferred_model and agent.last_interactive_model_used != agent.preferred_model
            else ""
        )
        return f"[modelo: {agent.last_interactive_model_used}{fallback}]"
    return "[modelo: sin llamada LLM; respuesta determinista]"


def print_help() -> None:
    print(
        textwrap.dedent(
            """
            Comandos:
              /help, /ayuda, /comandos
                                      Muestra esta ayuda completa
              /configuracion          Ver configuracion activa y como cambiar Home Assistant
              /configuracion hogar <url> | <token>
                                      Guarda nuevo Home Assistant en .env para el proximo arranque
              /estado                 Ver estado resumido de Codexon
              /ntop                   Monitor de worker, cola, ejecuciones y coste
              /salud                  Diagnostico rapido del sistema
              /herramientas           Ver herramientas MCP e internas disponibles
              /router                 Ver configuracion de ModelRouter
              /model                  Listar y seleccionar modelo IA interactivo
              /model pregunta <texto> Guardar una pregunta para comparar modelos
              /model probar           Repetir la pregunta guardada con el modelo seleccionado
              /coste                  Ver tokens y coste estimado
              /logs [n]               Ver ultimas lineas del log runtime
              /memoria                Ver memorias recientes
              /sensores               Ver observaciones recientes
              /aprender <texto>       Guardar una memoria manual
              /consultalo [problema]  Crear una nota para que Codex revise/corrija Codexon
              /codex <petición>       Abrir Codex para enseñar o modificar Codexon
              /tareas                 Ver tareas pendientes
              /tareas --todo          Ver tambien tareas completadas/canceladas
              /cancelar <id>          Cancelar tarea
              /reintentar <id>        Reactivar tarea cancelada o fallida
              /editar <id> | <ISO> | <titulo> | <instruccion>
                                      Editar una tarea programada
              /agentes                Listar agentes especializados
              /agente descubrir       Recargar descubrimiento de agentes
              /agente iniciar <nombre>
              /agente detener <nombre>
              /agente reiniciar <nombre>
              /agente run <nombre>
              /ls <ruta>              Listar directorio permitido
              /leer <ruta>            Leer fichero de texto permitido
              /escribir <ruta> | <texto>
                                      Crear fichero de texto permitido
              /borrar <ruta>          Borrar fichero/directorio vacio
              /salir                  Cerrar

            Tambien puedes pedir en lenguaje natural: leer sensores, consultar historico,
            encender/apagar dispositivos, programar tareas, escribir ficheros o buscar en web.
            Si dices "consultalo: <problema>", Codexon preparara el caso para Codex.
            Con /codex <petición>, Codex se abre de forma interactiva y al salir vuelves al chat.
            """
        ).strip()
    )


def print_configuration(agent: CodexonAgent, config: RuntimeConfig, agent_manager: AgentManager) -> None:
    usage = agent.memory.usage_summary(limit=0)["total"]
    print(
        textwrap.dedent(
            f"""
            Configuracion Codexon
            Home Assistant MCP URL: {config.mcp_url or "no configurado"}
            Home Assistant base REST: {config.ha_base_url or "no configurado"}
            HA_TOKEN: {mask_secret(config.ha_token)}
            REST Home Assistant: {"activo" if agent.has_homeassistant_rest else "inactivo"}
            Acciones requieren confirmacion: {"si" if config.require_action_confirmation else "no"}

            Modelo default: {agent.router.describe()["default"]}
            Modelo interactivo: {agent.preferred_model or "automatico"}
            Rutas modelos: {config.model_routes_path}
            DB memoria: {config.db_path}
            Agents dir: {config.agents_dir}
            Agentes cargados: {len(agent_manager.agents)}
            Agentes activos: {len(agent_manager.active_agents())}

            Observador sensores: {"activo" if config.enable_sensor_loop else "desactivado"}
            Intervalo sensores: {config.poll_seconds}s
            Timeout tareas: {TASK_TIMEOUT_SECONDS}s
            Tareas pendientes: {len(agent.memory.list_tasks(include_done=False, limit=100))}

            FS permitido: {", ".join(str(root) for root in config.fs_roots)}
            Log runtime: {Path(DEFAULT_LOG_FILE).expanduser()}
            Historial consola: {history_file_path()}
            Herramientas disponibles: {len(agent.tool_names)}
            Coste estimado acumulado: ${usage['cost']:.6f}
            .env: {Path('.env').resolve()}

            Cambiar Home Assistant para el proximo arranque:
            /configuracion hogar https://nuevo-home-assistant/api/mcp | TOKEN
            """
        ).strip()
    )


def handle_configuration_command(rest: str, agent: CodexonAgent, config: RuntimeConfig, agent_manager: AgentManager) -> None:
    text = rest.strip()
    if not text or text in {"ver", "mostrar", "show"}:
        print_configuration(agent, config, agent_manager)
        return
    action, _, payload = text.partition(" ")
    if action not in {"hogar", "home", "ha", "homeassistant"}:
        print("Uso: /configuracion hogar <HA_MCP_URL> | <HA_TOKEN>")
        return
    parts = [part.strip() for part in payload.split("|", 1)]
    if not parts or not parts[0]:
        print("Uso: /configuracion hogar <HA_MCP_URL> | <HA_TOKEN>")
        return
    updates = {"HA_MCP_URL": parts[0]}
    if len(parts) > 1 and parts[1]:
        updates["HA_TOKEN"] = parts[1]
    write_env_values(updates)
    print("Configuracion guardada en .env.")
    print("Reinicia Codexon para conectar al nuevo Home Assistant.")
    print(f"Nuevo HA_MCP_URL: {updates['HA_MCP_URL']}")
    print(f"Nuevo HA_TOKEN: {mask_secret(updates.get('HA_TOKEN') or config.ha_token)}")


def print_health(agent: CodexonAgent, agent_manager: AgentManager, config: RuntimeConfig) -> None:
    usage = agent.memory.usage_summary(limit=0)["total"]
    events = agent.memory.recent_events(limit=5)
    failed_tasks = [
        row for row in agent.memory.list_tasks(include_done=True, limit=100) if row["status"] == "failed"
    ]
    load_errors = len(agent_manager.load_errors)
    agent_rows = agent_manager.list_agents()
    failures = sum(row["stats"]["failures"] for row in agent_rows)
    status = "OK"
    if load_errors or failures or failed_tasks:
        status = "WARNING"
    print(
        textwrap.dedent(
            f"""
            Salud Codexon: {status}
            MCP: conectado
            ModelRouter: {config.model_routes_path}
            Agentes: {len(agent_rows)} cargados, {len(agent_manager.active_agents())} activos, {failures} fallos
            Errores de carga agentes: {load_errors}
            Tareas fallidas: {len(failed_tasks)}
            Coste estimado acumulado: ${usage['cost']:.6f}
            Log: {Path(DEFAULT_LOG_FILE).expanduser()}
            """
        ).strip()
    )
    if events:
        print("\nÚltimos eventos:")
        for event in events:
            print(f"- {event['created_at']} [{event['level']}] {event['message']}")


async def terminal_loop(
    agent: CodexonAgent,
    config: RuntimeConfig,
    stop: asyncio.Event,
    agent_manager: AgentManager,
) -> None:
    print(f"\n{APP_NAME} listo. Escribe /help para comandos.")
    while not stop.is_set():
        try:
            user_text = (await async_input("\nCodexon> ")).strip()
        except (EOFError, KeyboardInterrupt):
            stop.set()
            break

        if not user_text:
            continue

        command, _, rest = user_text.partition(" ")
        if command in {"/salir", "/exit", "/quit"}:
            stop.set()
            break
        if not command.startswith("/"):
            try:
                triggered = agent.activate_phrase_trigger(user_text)
            except Exception as exc:  # noqa: BLE001
                print(f"Error activando trigger por frase: {exc}")
                continue
            if triggered:
                print(triggered)
                continue
        if command in {"/help", "/ayuda", "/comandos"}:
            print_help()
            continue
        if command in {"/configuracion", "/configuración", "/config"}:
            handle_configuration_command(rest, agent, config, agent_manager)
            continue
        if command == "/memoria":
            print_rows("Memoria", agent.memory.recent_memories(20), "Sin memorias guardadas.")
            continue
        if command == "/sensores":
            print_rows("Observaciones", agent.memory.recent_observations(20), "Sin observaciones guardadas.")
            continue
        if command == "/tareas":
            print_tasks(agent.memory.list_tasks(include_done="--todo" in rest, limit=50))
            continue
        if command in {"/ntop", "/top"} or normalize_cancellation_key(user_text) in {"ntop", "top tareas", "monitor tareas", "monitor de tareas"}:
            print(format_scheduler_monitor(scheduler_monitor(agent.memory.conn, recent_limit=8)))
            continue
        if command == "/agentes":
            print(json.dumps(agent_manager.list_agents(), ensure_ascii=False, indent=2))
            if agent_manager.load_errors:
                print("\nErrores de carga:")
                print(json.dumps(agent_manager.load_errors, ensure_ascii=False, indent=2))
            continue
        if command == "/agente":
            parts = rest.split()
            action = parts[0] if parts else ""
            name = parts[1] if len(parts) > 1 else ""
            if action == "descubrir":
                agent_manager.discover()
                print("Agentes redescubiertos.")
                continue
            if not name or action not in {"iniciar", "detener", "reiniciar", "run"}:
                print("Uso: /agente iniciar|detener|reiniciar|run <nombre> o /agente descubrir")
                continue
            if action == "iniciar":
                print("Agente iniciado." if await agent_manager.start(name) else "No encontré ese agente.")
                continue
            if action == "detener":
                print("Agente detenido." if await agent_manager.stop(name) else "No encontré ese agente.")
                continue
            if action == "reiniciar":
                print("Agente reiniciado." if await agent_manager.restart(name) else "No encontré ese agente.")
                continue
            if action == "run":
                result = await agent_manager.run_once(name)
                print(json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2))
                continue
        if command == "/cancelar":
            try:
                task_id = int(rest.strip())
            except ValueError:
                print("Uso: /cancelar <id>")
                continue
            print("Tarea cancelada." if agent.memory.cancel_task(task_id) else "No encontré esa tarea.")
            continue
        if command == "/reintentar":
            try:
                task_id = int(rest.strip())
            except ValueError:
                print("Uso: /reintentar <id>")
                continue
            updated = agent.memory.update_task(task_id=task_id, status="pending")
            print("Tarea reactivada." if updated else "No encontré esa tarea.")
            continue
        if command == "/editar":
            parts = [part.strip() for part in rest.split("|")]
            if len(parts) != 4:
                print("Uso: /editar <id> | <ISO> | <titulo> | <instruccion>")
                continue
            try:
                task_id = int(parts[0])
            except ValueError:
                print("El id debe ser numérico.")
                continue
            updated = agent.memory.update_task(
                task_id=task_id,
                run_at=parts[1],
                title=parts[2],
                instruction=parts[3],
                status="pending",
            )
            print("Tarea editada." if updated else "No encontré esa tarea.")
            continue
        if command == "/herramientas":
            print("\nHerramientas MCP:")
            for name in agent.tool_names:
                print(f"- {name}")
            continue
        if command == "/coste":
            print_usage_summary(agent.memory.usage_summary(limit=12))
            continue
        if command == "/logs":
            try:
                limit = int(rest.strip() or "50")
            except ValueError:
                limit = 50
            lines = tail_file(Path(DEFAULT_LOG_FILE).expanduser(), limit=limit)
            if not lines:
                print("Sin logs todavía.")
            else:
                print("\n".join(lines))
            continue
        if command == "/salud":
            print_health(agent, agent_manager, config)
            continue
        if command in {"/model", "/modelo", "/models"}:
            model_action, _, model_payload = rest.strip().partition(" ")
            if not model_action:
                await print_model_catalog_pages(agent.router, agent.preferred_model)
                continue
            if model_action.lower() in {"auto", "automatico", "automático", "router"}:
                agent.preferred_model = None
                agent.memory.set_setting("interactive_model", None)
                print("Modelo interactivo en modo automatico.")
                continue
            if model_action.lower() in {"pagina", "page"}:
                try:
                    page = int(model_payload.strip())
                except ValueError:
                    print("Uso: /model pagina <numero>")
                    continue
                await print_model_catalog_pages(agent.router, agent.preferred_model, start_page=page)
                continue
            if model_action.lower() in {"configurados", "configured"}:
                print(format_model_menu(agent.router, agent.preferred_model, configured_only=True))
                continue
            if model_action.lower() in {"todos", "oficiales", "official"}:
                await print_model_catalog_pages(agent.router, agent.preferred_model)
                continue
            if model_action.lower() in {"actualizar", "refresh", "reload"}:
                refreshed_catalog = await fetch_openrouter_model_catalog()
                if not refreshed_catalog:
                    print("No pude actualizar el catalogo de OpenRouter; conservo el anterior.")
                    continue
                agent.router.model_catalog = refreshed_catalog
                print(f"Catalogo oficial actualizado: {len(refreshed_catalog)} modelos.")
                await print_model_catalog_pages(agent.router, agent.preferred_model)
                continue
            if model_action.lower() in {"buscar", "search"}:
                query = model_payload.strip()
                if not query:
                    print("Uso: /model buscar <texto>")
                    continue
                matches = agent.router.search_models(query, limit=20)
                if not matches:
                    print("No encontre modelos para esa busqueda.")
                    continue
                print(format_model_search_results(agent.router, query, matches))
                continue
            if model_action.lower() in {"pregunta", "question"}:
                question = model_payload.strip()
                if not question:
                    print(f"Pregunta guardada: {agent.model_test_question or 'ninguna'}")
                    continue
                agent.model_test_question = question
                agent.memory.set_setting("model_test_question", question)
                print("Pregunta de prueba guardada. Usa /model probar tras seleccionar cada modelo.")
                continue
            if model_action.lower() in {"probar", "test"}:
                question = model_payload.strip() or agent.model_test_question
                if not question:
                    print("Uso: /model pregunta <texto> y despues /model probar")
                    continue
                if model_payload.strip():
                    agent.model_test_question = question
                    agent.memory.set_setting("model_test_question", question)
                try:
                    answer = await await_with_terminal_heartbeat(
                        agent.ask(question, preferred_model=agent.preferred_model),
                        label="modelo",
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"Error probando modelo: {exc}")
                    continue
                print(f"\nPregunta: {question}\n\n{answer}\n{model_used_text(agent)}")
                continue

            selected_model = resolve_model_reference(agent.router, rest.strip())
            if not selected_model:
                matches = agent.router.search_models(rest.strip(), limit=20)
                if matches:
                    print(format_model_search_results(agent.router, rest.strip(), matches))
                else:
                    print("Modelo no encontrado. Usa /model o /model buscar <texto>.")
                continue
            metadata = agent.router.model_catalog.get(selected_model) or {}
            if metadata and not metadata.get("supports_tools"):
                print("Ese modelo no declara soporte de herramientas y no puede usarse en el chat operativo de Codexon.")
                continue
            agent.preferred_model = selected_model
            agent.memory.set_setting("interactive_model", selected_model)
            print(f"Modelo interactivo seleccionado: {selected_model}")
            print("La seleccion queda guardada. Usa /model pregunta y /model probar para compararlo.")
            continue
        if command == "/router":
            print(json.dumps(agent.router.describe(), ensure_ascii=False, indent=2))
            continue
        if command == "/ls":
            path = rest.strip() or "."
            try:
                print(await call_indexed_builtin_tool(agent, "fs_list_dir", {"path": path, "limit": 100}))
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if command == "/leer":
            path = rest.strip()
            if not path:
                print("Uso: /leer <ruta>")
                continue
            try:
                result = json.loads(await call_indexed_builtin_tool(agent, "fs_read_file", {"path": path, "max_chars": 20000}))
                print(result["content"])
                if result.get("truncated"):
                    print("\n[truncado]")
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if command == "/escribir":
            parts = [part.strip() for part in rest.split("|", 1)]
            if len(parts) != 2:
                print("Uso: /escribir <ruta> | <texto>")
                continue
            try:
                print(await call_indexed_builtin_tool(agent, "fs_write_file", {"path": parts[0], "content": parts[1], "overwrite": False}))
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if command == "/borrar":
            path = rest.strip()
            if not path:
                print("Uso: /borrar <ruta>")
                continue
            try:
                print(await call_indexed_builtin_tool(agent, "fs_delete_path", {"path": path, "confirm": True, "recursive": False}))
            except Exception as exc:  # noqa: BLE001
                print(f"Error: {exc}")
            continue
        if command == "/estado":
            print(
                textwrap.dedent(
                    f"""
                    Router modelos: {config.model_routes_path}
                    Modelo default: {agent.router.describe()["default"]}
                    Modelo interactivo: {agent.preferred_model or "automatico"}
                    DB: {config.db_path}
                    Agents dir: {config.agents_dir}
                    Agentes activos: {len(agent_manager.active_agents())}
                    Observador: {"activo" if config.enable_sensor_loop else "desactivado"}
                    Intervalo sensores: {config.poll_seconds}s
                    Herramientas MCP: {len(agent.tool_names)}
                    Tareas pendientes: {len(agent.memory.list_tasks(include_done=False, limit=100))}
                    Timeout tareas: {TASK_TIMEOUT_SECONDS}s
                    FS permitido: {", ".join(str(root) for root in config.fs_roots)}
                    Coste estimado: ${agent.memory.usage_summary(limit=0)["total"]["cost"]:.6f}
                    """
                ).strip()
            )
            continue
        if command == "/aprender":
            content = rest.strip()
            if not content:
                print("Uso: /aprender <texto>")
                continue
            agent.memory.add_memory(kind="hecho", topic="manual", content=content, confidence=1.0, source="manual")
            print("Memoria guardada.")
            continue

        if command == "/codex":
            run_codex_maintenance(rest)
            continue

        consultalo_instruction = parse_consultalo_request(user_text, command, rest)
        if consultalo_instruction is not None:
            create_teaching_note(consultalo_instruction)
            continue

        try:
            answer = await await_with_terminal_heartbeat(
                agent.ask(user_text, preferred_model=agent.preferred_model),
                label="orden",
            )
        except Exception as exc:  # noqa: BLE001
            agent.memory.add_event(level="error", message=f"Fallo respondiendo: {exc}")
            print(f"\nError: {exc}")
            save_last_interaction(
                {
                    "created_at": utc_now(),
                    "kind": "terminal_chat",
                    "instruction": user_text,
                    "error_class": "ERROR_AGENT_ASK",
                    "error": str(exc),
                }
            )
            continue
        print(f"\n{answer}")
        if agent.preferred_model:
            print(model_used_text(agent))
        save_last_interaction(
            {
                "created_at": utc_now(),
                "kind": "terminal_chat",
                "instruction": user_text,
                "final": {"status": "done", "result": answer},
                "error_class": "OK",
            }
        )


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agente terminal 24/7 con MCP Home Assistant, OpenRouter/DeepSeek y memoria local."
    )
    parser.add_argument("--db", default=os.getenv("CODEXON_DB", DEFAULT_DB), help="Ruta SQLite de memoria.")
    parser.add_argument(
        "--model-routes",
        default=os.getenv("CODEXON_MODEL_ROUTES", DEFAULT_MODEL_ROUTES),
        help="Archivo YAML/JSON con reglas de seleccion de modelos.",
    )
    parser.add_argument(
        "--agents-dir",
        default=os.getenv("CODEXON_AGENTS_DIR", DEFAULT_AGENTS_DIR),
        help="Directorio con modulos de agentes especializados.",
    )
    parser.add_argument(
        "--site-profile",
        default=os.getenv("CODEXON_SITE_PROFILE", DEFAULT_SITE_PROFILE),
        help="Perfil YAML local con roles, aliases y reglas de esta instalacion.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("CODEXON_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
        help="Intervalo de observación de sensores.",
    )
    parser.add_argument(
        "--no-sensor-loop",
        action="store_true",
        help="Desactiva el observador 24/7 de sensores.",
    )
    parser.add_argument(
        "--allow-actions-without-confirmation",
        action="store_true",
        help="Permite acciones MCP sin confirmación explícita previa.",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Arranca como servicio 24/7 sin prompt interactivo; mantiene tareas y observadores.",
    )
    parser.add_argument(
        "--mcp-url",
        default=os.getenv("HA_MCP_URL"),
        help="URL MCP HTTP de Home Assistant. También puede venir de HA_MCP_URL.",
    )
    parser.add_argument(
        "--ha-token",
        default=first_env("HA_TOKEN", "HOME_ASSISTANT_TOKEN", "HA_LONG_LIVED_TOKEN"),
        help="Token de larga duración de Home Assistant. También puede venir de HA_TOKEN/HOME_ASSISTANT_TOKEN.",
    )
    parser.add_argument(
        "--mcp-token",
        default=first_env("MCP_AUTH_TOKEN", "MCP_SERVER_API_KEY", "HA_TOKEN", "HOME_ASSISTANT_TOKEN", "HA_LONG_LIVED_TOKEN"),
        help="Token Bearer para el MCP HTTP. También puede venir de MCP_AUTH_TOKEN o MCP_SERVER_API_KEY.",
    )
    parser.add_argument(
        "--fs-roots",
        default=DEFAULT_FS_ROOTS,
        help="Rutas permitidas para leer/escribir/borrar, separadas por coma. Por defecto: directorio actual.",
    )
    parser.add_argument(
        "mcp_cmd",
        nargs=argparse.REMAINDER,
        help="Comando del servidor MCP después de --",
    )
    return parser.parse_args()


async def run_agent_session(
    *,
    session: ClientSession,
    client: AsyncOpenAI,
    memory: MemoryStore,
    config: RuntimeConfig,
    stop: asyncio.Event,
    router: ModelRouter,
    live_context: LiveContextManager,
) -> None:
    await session.initialize()
    listed = await session.list_tools()
    tools = listed.tools
    agent = CodexonAgent(
        client=client,
        router=router,
        session=session,
        memory=memory,
        tools=tools,
        require_action_confirmation=config.require_action_confirmation,
        ha_base_url=config.ha_base_url,
        ha_token=config.ha_token,
        fs_roots=config.fs_roots,
        site_profile=SiteProfile.load(config.site_profile_path),
    )
    agent_manager = AgentManager(
        config.agents_dir,
        context_services={
            "codexon": agent,
            "memory": memory,
            "router": router,
            "mcp_session": session,
            "live_context": live_context,
        },
        event_logger=runtime_log,
    )
    agent_manager.discover()

    print(f"Conectado a MCP con {len(tools)} herramientas.")
    print(f"Agentes descubiertos: {len(agent_manager.agents)}")
    runtime_log("info", "codexon", "started", mcp_tools=len(tools), agents=len(agent_manager.agents))
    tasks: list[asyncio.Task[Any]] = []
    if not config.service_mode:
        tasks.append(asyncio.create_task(terminal_loop(agent, config, stop, agent_manager)))
    else:
        print("Codexon servicio 24/7 activo: prompt interactivo desactivado.")
        tasks.append(asyncio.create_task(task_loop(agent, stop)))
        tasks.append(
            asyncio.create_task(
                agent_manager.run_configured(
                    stop,
                    Path(os.getenv("CODEXON_AGENT_CONFIG", "/data/codexon/agent_config.json")),
                )
            )
        )
        if config.ha_base_url and config.ha_token:
            tasks.append(
                asyncio.create_task(EventEngine(agent, runtime_log=runtime_log).run(stop))
            )
    if config.service_mode and config.enable_sensor_loop:
        tasks.append(
            asyncio.create_task(
                sensor_loop(agent, config.poll_seconds, config.sensor_prompt, stop)
            )
        )
    whatsapp_config = WhatsAppBridgeConfig.from_env()
    if config.service_mode and whatsapp_config.enabled:
        print("Canal WhatsApp interno de Codexon activo.")
        whatsapp_bridge = WhatsAppBridge(
            agent,
            config=whatsapp_config,
            log=runtime_log,
        )
        agent.whatsapp_bridge = whatsapp_bridge
        tasks.append(
            asyncio.create_task(
                whatsapp_bridge.run(stop)
            )
        )

    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> int:
    if load_dotenv is not None:
        load_dotenv()
    configure_runtime_defaults_from_env()
    setup_console_history()

    args = parse_args()
    if MISSING_DEPENDENCY:
        print(
            f"Falta la dependencia Python '{MISSING_DEPENDENCY}'. "
            "Instala primero con: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("Falta OPENROUTER_API_KEY. Ejemplo: export OPENROUTER_API_KEY='sk-or-...'", file=sys.stderr)
        return 2
    if args.mcp_cmd and args.mcp_cmd[0] == "--":
        args.mcp_cmd = args.mcp_cmd[1:]
    if not args.mcp_cmd and not args.mcp_url:
        print(
            "Falta conexión MCP. Usa HA_MCP_URL en .env, --mcp-url, o un comando después de --.",
            file=sys.stderr,
        )
        return 2

    config = RuntimeConfig(
        db_path=Path(args.db).expanduser(),
        model_routes_path=Path(args.model_routes).expanduser(),
        agents_dir=Path(args.agents_dir).expanduser(),
        site_profile_path=Path(args.site_profile).expanduser(),
        poll_seconds=max(30, args.poll_seconds),
        sensor_prompt=os.getenv(
            "CODEXON_SENSOR_PROMPT",
            "Consulta sensores de presencia, puertas, ventanas, movimiento, alarma y cámaras si existen.",
        ),
        enable_sensor_loop=not args.no_sensor_loop,
        require_action_confirmation=not args.allow_actions_without_confirmation,
        service_mode=args.service,
        mcp_url=args.mcp_url,
        mcp_cmd=args.mcp_cmd,
        ha_base_url=derive_ha_base_url(args.mcp_url),
        ha_token=args.ha_token,
        mcp_token=args.mcp_token,
        fs_roots=parse_fs_roots(args.fs_roots),
    )

    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    model_catalog = await fetch_openrouter_model_catalog()
    router = ModelRouter(config.model_routes_path, model_catalog)
    try:
        site_profile = SiteProfile.load(config.site_profile_path)
    except SiteProfileError as exc:
        print(f"Perfil local invalido: {exc}", file=sys.stderr)
        return 2
    memory = MemoryStore(config.db_path)
    live_context = LiveContextManager()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        if config.mcp_url:
            if not config.mcp_token:
                print(
                    "Falta token para conectar al MCP HTTP de Home Assistant. "
                    "Configura MCP_AUTH_TOKEN, MCP_SERVER_API_KEY, HA_TOKEN o HOME_ASSISTANT_TOKEN.",
                    file=sys.stderr,
                )
                return 2
            headers = {"Authorization": f"Bearer {config.mcp_token}"}
            async with streamablehttp_client(config.mcp_url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await run_agent_session(
                        session=session,
                        client=client,
                        memory=memory,
                        config=config,
                        stop=stop,
                        router=router,
                        live_context=live_context,
                    )
        else:
            server_params = StdioServerParameters(
                command=config.mcp_cmd[0],
                args=config.mcp_cmd[1:],
                env=os.environ.copy(),
            )
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await run_agent_session(
                        session=session,
                        client=client,
                        memory=memory,
                        config=config,
                        stop=stop,
                        router=router,
                        live_context=live_context,
                    )
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo conectar o mantener la sesión MCP: {exception_summary(exc)}", file=sys.stderr)
        if config.mcp_url and "supervisor" in config.mcp_url:
            if os.getenv("SUPERVISOR_TOKEN"):
                print(
                    "El host 'supervisor' resolvió, pero Home Assistant rechazó el token. "
                    "Revisa mcp_server_api_key, home_assistant_token o ha_long_lived_token en la configuración del add-on.",
                    file=sys.stderr,
                )
            else:
                print(
                    "La URL con host 'supervisor' normalmente solo resuelve dentro de Home Assistant. "
                    "Desde esta máquina usa la URL externa de HA, por ejemplo http://IP_DE_HA:8123/api/mcp.",
                    file=sys.stderr,
                )
        return 1
    finally:
        memory.close()

    print("\nCodexon cerrado.")
    runtime_log("info", "codexon", "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
