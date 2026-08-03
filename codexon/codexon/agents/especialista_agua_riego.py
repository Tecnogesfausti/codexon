from __future__ import annotations

from agents._specialist_support import entity_id, is_on, matches_any, resolve_ha_client, state_evidence
from agents.base import Agent, AgentContext, AgentFinding, AgentMetadata, AgentRunResult, expires_in, finding_result


WATER_TERMS = ("riego", "agua", "water", "caudal", "flow", "litro", "estanque", "bonsai")


class EspecialistaAguaRiegoAgent(Agent):
    metadata = AgentMetadata(
        name="especialista_agua_riego",
        description="Relaciona zonas de riego, válvulas, caudal, alarmas y volúmenes observados.",
        priority=90,
        entities=("switch.riego*", "sensor.*riego*", "binary_sensor.*riego*"),
        wake_events=("state_changed", "time_pattern"),
        frequency_seconds=180,
        daily_llm_budget_tokens=0,
    )

    async def run(self, context: AgentContext) -> AgentRunResult:
        try:
            states = await resolve_ha_client(context).get_states()
        except Exception as exc:  # noqa: BLE001
            finding = AgentFinding(self.name, "water_irrigation", "No puedo leer el sistema de agua y riego.", 0.0, (f"HA: {exc.__class__.__name__}",), urgency="warning", expires_at=expires_in(context.now, 90))
            return finding_result(context, finding, ok=False)

        relevant = [row for row in states if entity_id(row).split(".", 1)[0] in {"sensor", "binary_sensor", "switch"} and matches_any(row, WATER_TERMS)]
        valves = [row for row in relevant if entity_id(row).startswith("switch.") and "rele" in entity_id(row) and not matches_any(row, ("alarma", "habilitada", "luz"))]
        active = [row for row in valves if is_on(row)]
        alarms = [row for row in relevant if entity_id(row).startswith("binary_sensor.") and matches_any(row, ("alarma", "sin agua", "conflicto", "fuga")) and is_on(row)]
        volumes = [row for row in relevant if matches_any(row, ("ultimo riego", "último riego", "litro")) and str(row.get("state", "")).lower() not in {"unknown", "unavailable", "sin datos"}]
        if alarms:
            summary = f"Agua y riego: {len(alarms)} alarmas activas y {len(active)} válvulas abiertas."
            urgency, action, confidence = "critical", "Revisar la evidencia y pedir confirmación antes de cerrar válvulas o bombas.", 0.95
        elif active:
            summary = f"Agua y riego: {len(active)} válvulas activas; no hay alarmas de agua activas."
            urgency, action, confidence = "info", "Cruzar duración y contador de caudal para atribuir litros a cada zona.", 0.88
        else:
            summary = f"Agua y riego: {len(valves)} válvulas vigiladas, todas cerradas y sin alarmas activas."
            urgency, action, confidence = "info", None, 0.9
        evidence_rows = [*alarms[:6], *active[:8], *volumes[:8]]
        finding = AgentFinding(self.name, "water_irrigation", summary, confidence, tuple(state_evidence(row) for row in evidence_rows), tuple(entity_id(row) for row in relevant), recommended_action=action, urgency=urgency, expires_at=expires_in(context.now, self.frequency_seconds * 2))
        return finding_result(context, finding, extra={"entities_checked": len(relevant), "valves": len(valves), "active_valves": len(active), "active_alarms": len(alarms), "recent_volume_sensors": len(volumes)})
