from __future__ import annotations

from agents._specialist_support import entity_id, matches_any, number, resolve_ha_client, state_evidence
from agents.base import Agent, AgentContext, AgentFinding, AgentMetadata, AgentRunResult, expires_in, finding_result


WATCHED_DOMAINS = {"sensor", "binary_sensor", "switch", "light", "climate", "cover", "camera"}


class EspecialistaEstadoTecnicoAgent(Agent):
    metadata = AgentMetadata(
        name="especialista_estado_tecnico",
        description="Agrupa indisponibilidades, estados desconocidos y baterías bajas por sistema.",
        priority=80,
        entities=("sensor.*", "binary_sensor.*", "switch.*", "light.*", "climate.*", "cover.*"),
        wake_events=("state_changed", "time_pattern"),
        frequency_seconds=600,
        daily_llm_budget_tokens=0,
    )

    async def run(self, context: AgentContext) -> AgentRunResult:
        try:
            states = await resolve_ha_client(context).get_states()
        except Exception as exc:  # noqa: BLE001
            finding = AgentFinding(self.name, "technical_health", "No puedo revisar el estado técnico de Home Assistant.", 0.0, (f"HA: {exc.__class__.__name__}",), urgency="critical", expires_at=expires_in(context.now, 120))
            return finding_result(context, finding, ok=False)

        watched = [row for row in states if entity_id(row).split(".", 1)[0] in WATCHED_DOMAINS]
        unavailable = [row for row in watched if str(row.get("state", "")).lower() == "unavailable"]
        unknown = [row for row in watched if str(row.get("state", "")).lower() == "unknown"]
        low_battery = [row for row in watched if matches_any(row, ("battery", "bateria")) and number(row) is not None and str((row.get("attributes") or {}).get("unit_of_measurement") or "") == "%" and number(row) <= 20]
        affected = [*unavailable, *unknown, *low_battery]
        domains: dict[str, int] = {}
        for row in affected:
            domain = entity_id(row).split(".", 1)[0]
            domains[domain] = domains.get(domain, 0) + 1
        if affected:
            summary = f"Estado técnico: {len(unavailable)} unavailable, {len(unknown)} unknown y {len(low_battery)} baterías bajas."
            action = "Agrupar por dispositivo o integración y revisar primero los fallos que afecten seguridad, agua o climatización."
        else:
            summary = f"Estado técnico: {len(watched)} entidades revisadas sin incidencias básicas."
            action = None
        finding = AgentFinding(self.name, "technical_health", summary, 0.96, tuple(state_evidence(row) for row in affected[:20]), tuple(entity_id(row) for row in affected), recommended_action=action, urgency="warning" if affected else "info", expires_at=expires_in(context.now, self.frequency_seconds * 2))
        return finding_result(context, finding, extra={"entities_checked": len(watched), "unavailable": len(unavailable), "unknown": len(unknown), "low_battery": len(low_battery), "groups": domains})
