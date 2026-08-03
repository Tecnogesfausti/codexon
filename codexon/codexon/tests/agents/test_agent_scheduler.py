from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agents.base import Agent, AgentContext, AgentMetadata, AgentRunResult
from agents.manager import AgentManager, ManagedAgent


class CountingAgent(Agent):
    metadata = AgentMetadata(name="counting", description="test", frequency_seconds=1)

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, context: AgentContext) -> AgentRunResult:
        self.runs += 1
        return AgentRunResult(ok=True, message="ok")


class AgentSchedulerTest(unittest.TestCase):
    def test_configured_loop_runs_only_enabled_agents(self) -> None:
        async def scenario() -> tuple[int, bool]:
            with tempfile.TemporaryDirectory() as tempdir:
                config_path = Path(tempdir) / "agents.json"
                config_path.write_text(json.dumps({"agents": {"counting": {"enabled": True}}}), encoding="utf-8")
                agent = CountingAgent()
                manager = AgentManager(Path(tempdir))
                manager.agents[agent.name] = ManagedAgent(agent=agent, module_path=Path("counting.py"))
                stop = asyncio.Event()
                task = asyncio.create_task(manager.run_configured(stop, config_path, poll_seconds=0.01))
                await asyncio.sleep(0.05)
                stop.set()
                await task
                return agent.runs, manager.agents[agent.name].active

        runs, active = asyncio.run(scenario())
        self.assertEqual(runs, 1)
        self.assertTrue(active)
