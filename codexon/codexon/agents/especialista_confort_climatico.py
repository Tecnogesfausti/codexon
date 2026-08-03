from __future__ import annotations

from agents._specialist_support import entity_id, matches_any, number, resolve_ha_client, state_evidence
from agents.base import Agent, AgentContext, AgentFinding, AgentMetadata, AgentRunResult, expires_in, finding_result


class EspecialistaConfortClimaticoAgent(Agent):
    metadata = AgentMetadata(
        name="especialista_confort_climatico",
        description="Combina temperatura, humedad y CO₂ interior con el contexto meteorológico exterior.",
        priority=85,
        entities=("sensor.*", "climate.*", "cover.*"),
        wake_events=("state_changed", "time_pattern"),
        frequency_seconds=300,
        daily_llm_budget_tokens=0,
    )

    async def run(self, context: AgentContext) -> AgentRunResult:
        try:
            states = await resolve_ha_client(context).get_states()
        except Exception as exc:  # noqa: BLE001
            finding = AgentFinding(self.name, "climate_comfort", "No puedo leer el clima interior.", 0.0, (f"HA: {exc.__class__.__name__}",), urgency="warning", expires_at=expires_in(context.now, 120))
            return finding_result(context, finding, ok=False)
        excluded = ("aemet", "itorre", "meteostation", "riego", "battery", "bateria", "extern", "outdoor", "termodiamante", "forecast")
        numeric = [row for row in states if entity_id(row).startswith("sensor.") and number(row) is not None and not matches_any(row, excluded)]
        temperatures = [row for row in numeric if str((row.get("attributes") or {}).get("device_class") or "").lower() == "temperature"]
        humidity = [row for row in numeric if str((row.get("attributes") or {}).get("device_class") or "").lower() == "humidity"]
        co2 = [row for row in numeric if str((row.get("attributes") or {}).get("device_class") or "").lower() == "carbon_dioxide" or matches_any(row, ("co2", "carbon dioxide", "dioxido de carbono"))]
        climate_readings: list[tuple[dict, float]] = []
        for row in states:
            if not entity_id(row).startswith("climate."):
                continue
            value = (row.get("attributes") or {}).get("current_temperature")
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            climate_readings.append((row, parsed))
        temp_values = [number(row) for row in temperatures if number(row) is not None and -20 <= number(row) <= 60]
        temp_values.extend(value for _, value in climate_readings if -20 <= value <= 60)
        humidity_values = [number(row) for row in humidity if number(row) is not None and 0 <= number(row) <= 100]
        co2_values = [number(row) for row in co2 if number(row) is not None]
        evidence_rows = [*temperatures[:6], *humidity[:4], *co2[:4]]
        evidence = [state_evidence(row) for row in evidence_rows]
        evidence.extend(f"{entity_id(row)} current_temperature={value:g} °C" for row, value in climate_readings[:4])
        subjects = [entity_id(row) for row in evidence_rows]
        subjects.extend(entity_id(row) for row, _ in climate_readings[:4])
        problems: list[str] = []
        actions: list[str] = []
        if temp_values and max(temp_values) > 29:
            problems.append("alguna temperatura interior es alta")
            actions.append("comparar exterior e interior antes de ventilar o sombrear")
        if temp_values and min(temp_values) < 15:
            problems.append("alguna temperatura interior es baja")
            actions.append("revisar climatización y estancias afectadas")
        if humidity_values and max(humidity_values) > 70:
            problems.append("alguna humedad interior es alta")
            actions.append("valorar ventilación si el exterior es favorable")
        if co2_values and max(co2_values) > 1000:
            problems.append("CO₂ elevado")
            actions.append("ventilar la zona afectada")
        external_sources: tuple[str, ...] = ()
        live_context = context.services.get("live_context")
        if live_context is not None:
            try:
                external = await live_context.get_context(domains=["weather", "air_quality"], max_items_per_domain=5)
                external_sources = tuple(key for key in ("weather", "air_quality") if external.get(key))
            except Exception:  # noqa: BLE001
                external_sources = ()
        temperature_count = len(temperatures) + len(climate_readings)
        summary = "Confort climático: " + (", ".join(problems) + "." if problems else f"sin desviaciones básicas en {temperature_count} temperaturas interiores, {len(humidity)} humedades y {len(co2)} sensores de CO₂.")
        finding = AgentFinding(self.name, "climate_comfort", summary, 0.82 if evidence else 0.25, tuple(evidence), tuple(subjects), external_sources, "; ".join(actions) or None, "warning" if problems else "info", expires_in(context.now, self.frequency_seconds * 2))
        return finding_result(context, finding, extra={"temperature_count": temperature_count, "humidity_count": len(humidity), "co2_count": len(co2), "problem_count": len(problems)})
