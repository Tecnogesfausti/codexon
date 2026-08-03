from __future__ import annotations

from agents._specialist_support import entity_id, friendly_name, is_on, matches_any, resolve_ha_client, state_evidence
from agents.base import Agent, AgentContext, AgentFinding, AgentMetadata, AgentRunResult, expires_in, finding_result


SIGNAL_TERMS = ("presence", "presencia", "person", "persona", "motion", "movimiento", "infrarojo", "occupancy")


class EspecialistaPresenciaSeguridadAgent(Agent):
    metadata = AgentMetadata(
        name="especialista_presencia_seguridad",
        description="Fusiona radares, infrarrojos y deteccion de personas sin activar alarmas.",
        priority=95,
        entities=("binary_sensor.*",),
        wake_events=("state_changed",),
        frequency_seconds=60,
        daily_llm_budget_tokens=0,
    )

    async def run(self, context: AgentContext) -> AgentRunResult:
        try:
            states = await resolve_ha_client(context).get_states()
        except Exception as exc:  # noqa: BLE001
            finding = AgentFinding(self.name, "presence_security", "No puedo leer las señales de presencia.", 0.0, (f"HA: {exc.__class__.__name__}",), urgency="warning", expires_at=expires_in(context.now, 60))
            return finding_result(context, finding, ok=False)

        signals = [row for row in states if entity_id(row).startswith("binary_sensor.") and matches_any(row, SIGNAL_TERMS)]
        active = [row for row in signals if is_on(row)]
        unavailable = [row for row in signals if str(row.get("state", "")).lower() in {"unknown", "unavailable"}]
        modalities = {"camera" if matches_any(row, ("camara", "person")) else "radar" if matches_any(row, ("radar", "sonar")) else "infrared" for row in active}
        confidence = 0.25 if not signals else min(0.95, 0.52 + 0.1 * len(active) + 0.12 * max(0, len(modalities) - 1))
        if active:
            names = ", ".join(friendly_name(row) for row in active[:4])
            summary = f"Presencia: {len(active)} de {len(signals)} señales activas ({names})."
            action = "Pedir a Codexon que correlacione zona, horario y secuencia antes de considerar una alerta."
        else:
            summary = f"Presencia: ninguna de {len(signals)} señales está activa."
            action = None
        finding = AgentFinding(
            self.name, "presence_security", summary, confidence,
            tuple(state_evidence(row) for row in [*active[:8], *unavailable[:4]]),
            tuple(entity_id(row) for row in signals),
            recommended_action=action,
            urgency="warning" if active else "info",
            expires_at=expires_in(context.now, self.frequency_seconds * 2),
        )
        return finding_result(context, finding, extra={"signals_checked": len(signals), "active_count": len(active), "unavailable_count": len(unavailable), "modalities": sorted(modalities)})
