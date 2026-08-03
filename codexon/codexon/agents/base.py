from __future__ import annotations

import datetime as dt
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentMetadata:
    name: str
    description: str
    priority: int = 50
    entities: tuple[str, ...] = ()
    wake_events: tuple[str, ...] = ()
    frequency_seconds: int = 3600
    daily_llm_budget_tokens: int = 0
    recommended_model: str | None = None


@dataclass
class AgentContext:
    now: dt.datetime
    services: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRunResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentFinding:
    """Common, read-only evidence contract produced by specialist agents."""

    agent: str
    category: str
    finding: str
    confidence: float
    evidence: tuple[str, ...] = ()
    subject_entities: tuple[str, ...] = ()
    external_sources: tuple[str, ...] = ()
    recommended_action: str | None = None
    urgency: str = "info"
    expires_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "category": self.category,
            "finding": self.finding,
            "confidence": round(max(0.0, min(1.0, self.confidence)), 3),
            "evidence": list(self.evidence),
            "subject_entities": list(self.subject_entities),
            "external_sources": list(self.external_sources),
            "recommended_action": self.recommended_action,
            "urgency": self.urgency,
            "expires_at": self.expires_at,
        }


def finding_result(
    context: AgentContext,
    finding: AgentFinding,
    *,
    ok: bool = True,
    extra: dict[str, Any] | None = None,
    save: bool = True,
) -> AgentRunResult:
    """Build and optionally persist a specialist result without taking actions."""

    data = {"contract": "codexon.agent_finding.v1", "finding": finding.as_dict()}
    if extra:
        data.update(extra)
    result = AgentRunResult(ok=ok, message=finding.finding, data=data)
    memory = context.services.get("memory")
    if save and memory is not None and hasattr(memory, "add_observation"):
        signature = json.dumps(
            {
                "finding": finding.finding,
                "urgency": finding.urgency,
                "recommended_action": finding.recommended_action,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        setting_key = f"agents.last_finding.{finding.agent}"
        previous = memory.get_setting(setting_key, "") if hasattr(memory, "get_setting") else ""
        data["deduplicated"] = previous == signature
        if previous != signature:
            memory.add_observation(
                source=finding.agent,
                summary=finding.finding,
                raw=json.dumps(data, ensure_ascii=False, default=str),
            )
            if hasattr(memory, "set_setting"):
                memory.set_setting(setting_key, signature)
    return result


def expires_in(now: dt.datetime, seconds: int) -> str:
    return (now + dt.timedelta(seconds=max(1, seconds))).isoformat(timespec="seconds")


class Agent(ABC):
    metadata: AgentMetadata

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def priority(self) -> int:
        return self.metadata.priority

    @property
    def entities(self) -> tuple[str, ...]:
        return self.metadata.entities

    @property
    def frequency_seconds(self) -> int:
        return self.metadata.frequency_seconds

    @abstractmethod
    async def run(self, context: AgentContext) -> AgentRunResult:
        """Execute one monitoring pass."""
