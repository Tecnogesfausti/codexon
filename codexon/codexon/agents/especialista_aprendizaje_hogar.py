from __future__ import annotations

from agents.base import Agent, AgentContext, AgentFinding, AgentMetadata, AgentRunResult, expires_in, finding_result


class EspecialistaAprendizajeHogarAgent(Agent):
    metadata = AgentMetadata(
        name="especialista_aprendizaje_hogar",
        description="Audita alias, sustituciones, zonas y relaciones aprendidas para detectar huecos o contradicciones.",
        priority=75,
        entities=(),
        wake_events=("memory_changed", "time_pattern"),
        frequency_seconds=1800,
        daily_llm_budget_tokens=0,
    )

    async def run(self, context: AgentContext) -> AgentRunResult:
        memory = context.services.get("memory")
        conn = getattr(memory, "conn", None)
        if conn is None:
            finding = AgentFinding(self.name, "home_learning", "No puedo auditar el aprendizaje porque la memoria no está disponible.", 0.0, urgency="warning", expires_at=expires_in(context.now, 300))
            return finding_result(context, finding, ok=False)
        counts: dict[str, int] = {}
        for label, table in (("catalog", "entity_catalog"), ("aliases", "entity_aliases"), ("locations", "entity_locations"), ("roles", "entity_roles"), ("teachings", "entity_teachings")):
            try:
                counts[label] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception:  # noqa: BLE001
                counts[label] = 0
        conflicts = list(conn.execute("""
            SELECT normalized_alias, COUNT(DISTINCT entity_id) AS entities
            FROM entity_aliases
            WHERE priority >= 80
              AND normalized_alias NOT IN ('none', 'sin nombre', 'unknown')
            GROUP BY normalized_alias
            HAVING COUNT(DISTINCT entity_id) > 1
            ORDER BY entities DESC, normalized_alias
            LIMIT 12
        """)) if counts["aliases"] else []
        orphan_replacements = list(conn.execute("""
            SELECT key_text, target_entity_id
            FROM entity_teachings t
            LEFT JOIN entity_catalog c ON c.entity_id = t.target_entity_id
            WHERE t.active = 1 AND t.teaching_type = 'replacement' AND c.entity_id IS NULL
            LIMIT 12
        """)) if counts["teachings"] else []
        problems = len(conflicts) + len(orphan_replacements)
        summary = f"Aprendizaje: {counts['catalog']} entidades, {counts['aliases']} alias, {counts['locations']} ubicaciones, {counts['roles']} roles y {counts['teachings']} enseñanzas; {problems} conflictos a revisar."
        evidence = tuple([f"alias ambiguo: {row[0]} -> {row[1]} entidades" for row in conflicts] + [f"sustitución sin entidad: {row[0]} -> {row[1]}" for row in orphan_replacements])
        finding = AgentFinding(self.name, "home_learning", summary, 0.99, evidence, recommended_action="Resolver las ambigüedades mediante una corrección explícita del usuario." if problems else None, urgency="warning" if problems else "info", expires_at=expires_in(context.now, self.frequency_seconds * 2))
        return finding_result(context, finding, extra={"counts": counts, "alias_conflicts": [dict(row) for row in conflicts], "orphan_replacements": [dict(row) for row in orphan_replacements]})
