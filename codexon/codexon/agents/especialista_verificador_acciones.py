from __future__ import annotations

import json

from agents._specialist_support import entity_id, resolve_ha_client, state_evidence
from agents.base import Agent, AgentContext, AgentFinding, AgentMetadata, AgentRunResult, expires_in, finding_result


class EspecialistaVerificadorAccionesAgent(Agent):
    metadata = AgentMetadata(
        name="especialista_verificador_acciones",
        description="Comprueba el estado final esperado de las acciones registradas por Codexon.",
        priority=100,
        entities=("*",),
        wake_events=("state_changed", "task_finished"),
        frequency_seconds=30,
        daily_llm_budget_tokens=0,
    )

    async def run(self, context: AgentContext) -> AgentRunResult:
        memory = context.services.get("memory")
        raw = memory.get_setting("agents.pending_verifications", "[]") if memory is not None and hasattr(memory, "get_setting") else "[]"
        try:
            pending = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            pending = []
        pending = [item for item in pending if isinstance(item, dict) and item.get("entity_id") and "expected_state" in item]
        if not pending:
            finding = AgentFinding(self.name, "action_verification", "Verificación: no hay acciones pendientes de comprobar.", 1.0, urgency="info", expires_at=expires_in(context.now, self.frequency_seconds * 2))
            return finding_result(context, finding)
        try:
            states = await resolve_ha_client(context).get_states()
        except Exception as exc:  # noqa: BLE001
            finding = AgentFinding(self.name, "action_verification", "No puedo verificar las acciones pendientes.", 0.0, (f"HA: {exc.__class__.__name__}",), urgency="warning", expires_at=expires_in(context.now, 30))
            return finding_result(context, finding, ok=False)
        by_id = {entity_id(row): row for row in states}
        verified: list[dict] = []
        failed: list[dict] = []
        missing: list[dict] = []
        for item in pending:
            state = by_id.get(str(item["entity_id"]))
            if state is None:
                missing.append(item)
            elif str(state.get("state")) == str(item["expected_state"]):
                verified.append(item)
            else:
                failed.append({**item, "actual_state": state.get("state")})
        summary = f"Verificación: {len(verified)} correctas, {len(failed)} con estado inesperado y {len(missing)} entidades ausentes."
        evidence = [state_evidence(by_id[str(item["entity_id"])]) for item in [*verified, *failed] if str(item["entity_id"]) in by_id]
        action = "No repetir automáticamente; pedir a Codexon que explique el fallo y solicite confirmación." if failed or missing else None
        finding = AgentFinding(self.name, "action_verification", summary, 0.98, tuple(evidence), tuple(str(item["entity_id"]) for item in pending), recommended_action=action, urgency="warning" if failed or missing else "info", expires_at=expires_in(context.now, self.frequency_seconds * 2))
        if memory is not None and hasattr(memory, "set_setting"):
            memory.set_setting("agents.pending_verifications", "[]")
        return finding_result(context, finding, extra={"verified": verified, "failed": failed, "missing": missing})
