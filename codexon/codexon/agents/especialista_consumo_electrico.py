"""Explain evidence from the persistent consumption learner, with no extra HA/LLM calls."""
import datetime as dt
import json
from agents.base import Agent, AgentMetadata, AgentFinding, finding_result, expires_in
from services.security_visual import timestamp

class EspecialistaConsumoElectricoAgent(Agent):
    metadata=AgentMetadata(name='especialista_consumo_electrico',description='Aprende patrones de consumo por circuito, hora y día; detecta desviaciones y calcula escenarios de ahorro.',priority=80,frequency_seconds=300,daily_llm_budget_tokens=0)
    async def run(self,context):
        memory=context.services.get('memory')
        snapshot=json.loads(memory.get_setting('home_monitor.snapshot','{}')) if memory else {}
        fresh=context.now.timestamp()-(timestamp(snapshot.get('updated_at')) or 0)<180
        energy=snapshot.get('energy',{})
        alerts=energy.get('alerts',[]);circuits=energy.get('circuits',[])
        if not fresh or not circuits:
            finding=AgentFinding(self.name,'energy_learning','Consumos: faltan medidas recientes del supervisor.',0.,urgency='warning',expires_at=expires_in(context.now,300))
        else:
            summary=f"Electricidad: {len(circuits)} circuitos, {energy['total_power_w']} W, {energy['today_kwh']} kWh hoy; {len(alerts)} desviaciones del patrón habitual."
            finding=AgentFinding(self.name,'energy_learning',summary,.9,tuple(a['message'] for a in alerts),tuple(c['entity_id'] for c in circuits),recommended_action='Identificar las cargas responsables en Agua y electricidad.' if alerts else None,urgency='warning' if alerts else 'info',expires_at=expires_in(context.now,600))
        return finding_result(context,finding,extra={'energy':energy})
