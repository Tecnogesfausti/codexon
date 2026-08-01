from __future__ import annotations

import os
from dataclasses import dataclass, field

from services.live_context.models import LocationConfig


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class LiveContextConfig:
    location: LocationConfig = field(
        default_factory=lambda: LocationConfig(
            name=os.getenv("LIVE_CONTEXT_LOCATION") or "Home",
            lat=env_float("LIVE_CONTEXT_LAT", 0.0),
            lon=env_float("LIVE_CONTEXT_LON", 0.0),
            radius_km=env_float("LIVE_CONTEXT_RADIUS_KM", 20.0),
            timezone=os.getenv("LIVE_CONTEXT_TIMEZONE") or "UTC",
        )
    )
    http_timeout_seconds: float = float(os.getenv("LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS", "15"))
    max_retries: int = int(os.getenv("LIVE_CONTEXT_MAX_RETRIES", "2"))
    max_concurrency: int = int(os.getenv("LIVE_CONTEXT_MAX_CONCURRENCY", "4"))
    open_meteo_enabled: bool = os.getenv("OPEN_METEO_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    dgt_traffic_enabled: bool = os.getenv("DGT_TRAFFIC_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    dgt_traffic_url: str = os.getenv("DGT_TRAFFIC_URL", "")
    open_meteo_air_quality_enabled: bool = os.getenv("OPEN_METEO_AIR_QUALITY_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

    @property
    def air_quality_locations(self) -> list[LocationConfig]:
        return parse_air_quality_locations(os.getenv("AIR_QUALITY_LOCATIONS", ""), self.location)

    @classmethod
    def from_env(cls) -> "LiveContextConfig":
        return cls()


def parse_air_quality_locations(value: str, fallback: LocationConfig) -> list[LocationConfig]:
    rows: list[LocationConfig] = []
    for item in value.split(';'):
        parts = [part.strip() for part in item.split('|')]
        if len(parts) != 3 or not all(parts):
            continue
        try:
            rows.append(LocationConfig(parts[0], float(parts[1]), float(parts[2]), fallback.radius_km, fallback.timezone))
        except ValueError:
            continue
    return rows or [fallback]
