import asyncio
import curses
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch


loader = importlib.machinery.SourceFileLoader("codemux_app", str(Path(__file__).resolve().parents[1] / "codemux"))
spec = importlib.util.spec_from_loader(loader.name, loader)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
loader.exec_module(app)


class Screen:
    def __init__(self):
        self.lines = {}

    def getmaxyx(self):
        return 24, 80

    def erase(self):
        self.lines.clear()

    def addnstr(self, row, column, text, width, style):
        self.lines[row] = text[:width]

    def refresh(self):
        pass


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agents = [app.Agent(f"agent-{i}", f"host-{i}", "codex") for i in range(3)]
        self.screen = Screen()
        self.dashboard = app.Dashboard(self.screen, self.agents)
        for agent in self.agents:
            self.dashboard.observations[agent].update({
                "turn": "finished", "goal": "blocked", "thread_id": agent.name,
                "response": {"id": "reply", "text": ("A long response line\n" * 40), "kind": "final", "timestamp": 100},
            })

    async def test_expand_all_from_partial_then_collapse_all(self):
        await self.dashboard.handle(ord(" "))
        self.assertEqual(self.dashboard.expanded, {self.agents[0]})
        await self.dashboard.handle(ord("a"))
        self.assertEqual(self.dashboard.expanded, set(self.agents))
        await self.dashboard.handle(ord("a"))
        self.assertEqual(self.dashboard.expanded, set())

    async def test_paging_long_response_and_selecting_next_agent(self):
        await self.dashboard.handle(ord("a"))
        self.dashboard.draw()
        await self.dashboard.handle(curses.KEY_NPAGE)
        self.dashboard.draw()
        self.assertGreater(self.dashboard.top, 0)
        old_top = self.dashboard.top
        self.dashboard.draw()
        self.assertEqual(self.dashboard.top, old_top)
        await self.dashboard.handle(ord("j"))
        self.dashboard.draw()
        self.assertTrue(any("agent-1" in line for row, line in self.screen.lines.items() if 3 <= row < 17))
        await self.dashboard.handle(ord("a"))
        self.dashboard.draw()
        self.assertEqual(self.dashboard.top, 0)

    async def test_enter_attaches_instead_of_toggling_response(self):
        with patch.object(self.dashboard, "attach") as attach:
            await self.dashboard.handle(10)
        attach.assert_awaited_once()
        self.assertEqual(self.dashboard.expanded, set())

    async def test_status_failure_preserves_explicitly_stale_preview(self):
        observation = self.dashboard.observations[self.agents[0]]
        observation.update({"error": "SSH disconnected"})
        self.assertEqual(observation.turn, "stale")
        self.assertEqual(observation.goal, "-")
        preview = "\n".join(self.dashboard.response_lines(self.agents[0], 80))
        self.assertIn("Last cached final response", preview)
        self.assertIn("SSH disconnected", preview)
        self.assertIn("A long response line", preview)
        observation.update({"thread_id": None, "turn": "absent", "goal": None, "response": None})
        self.assertNotIn("A long response line", "\n".join(self.dashboard.response_lines(self.agents[0], 80)))

    async def test_port_edit_does_not_trigger_expand_all(self):
        await self.dashboard.handle(ord("w"))
        await self.dashboard.handle(ord("a"))
        self.assertEqual(self.dashboard.expanded, set())
        self.assertEqual(self.dashboard.port_input, "8000")

    async def test_expanded_preview_refreshes_and_labels_cached_progress(self):
        agent = self.agents[0]
        observation = self.dashboard.observations[agent]
        await self.dashboard.handle(ord(" "))
        for text in ("Inspecting changes", "Running checks"):
            observation.update({"turn": "active", "goal": None,
                                "response": {"id": "progress", "kind": "progress", "text": text, "timestamp": 120}})
            self.dashboard.draw()
            preview = "\n".join(self.screen.lines.values())
            self.assertIn(text, preview)
            self.assertIn("Last progress message / 1970-01-01 00:02 UTC", preview)
            self.assertNotIn("Last final response", preview)
        observation.update({"error": "SSH disconnected"})
        preview = "\n".join(self.dashboard.response_lines(agent, 80))
        self.assertIn("Last cached progress message", preview)
        self.assertIn("Running checks", preview)
        observation.update({"turn": "finished", "goal": None,
                            "response": {"id": "done", "kind": "final", "text": "Checks passed", "timestamp": 180}})
        self.dashboard.draw()
        preview = "\n".join(self.screen.lines.values())
        self.assertIn("Last final response / 1970-01-01 00:03 UTC", preview)
        self.assertIn("Checks passed", preview)
        self.assertNotIn("Running checks", preview)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=3)

    async def test_streaming_snapshots_and_cancellation_clean_up_process(self):
        agents = [app.Agent("one", "same-host", "one"), app.Agent("two", "same-host", "two")]
        observations = {agent: app.Observation() for agent in agents}
        values = {agent.session: {"turn": "active", "goal": "active", "response": None} for agent in agents}
        source = (f"import asyncio\nprint({json.dumps(json.dumps(values))}, flush=True)\n"
                  "asyncio.run(asyncio.sleep(60))\n").encode()
        spawned = []
        original = asyncio.create_subprocess_exec

        async def local_process(*command, **kwargs):
            process = await original(sys.executable, "-u", "-", **kwargs)
            spawned.append(process)
            return process

        with patch.object(app.asyncio, "create_subprocess_exec", side_effect=local_process):
            task = asyncio.create_task(app.watch_host(agents, observations, source, AsyncMock(command=AsyncMock(side_effect=lambda: ["ssh", "host"])), app.Notifications()))
            try:
                await self.wait_until(lambda: all(value.turn == "active" for value in observations.values()))
                self.assertEqual(len(spawned), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertIsNotNone(spawned[0].returncode)

    async def test_reader_exit_marks_snapshot_stale_and_retains_response(self):
        agent = app.Agent("one", "host", "codex")
        observations = {agent: app.Observation()}
        values = {"codex": {"turn": "finished", "goal": "complete", "response": {"text": "Done"}}}
        source = (f"import sys\nprint({json.dumps(json.dumps(values))}, flush=True)\n"
                  "print('connection lost', file=sys.stderr)\nsys.exit(1)\n").encode()
        original = asyncio.create_subprocess_exec

        async def local_process(*command, **kwargs):
            return await original(sys.executable, "-u", "-", **kwargs)

        with patch.object(app.asyncio, "create_subprocess_exec", side_effect=local_process):
            task = asyncio.create_task(app.watch_host([agent], observations, source, AsyncMock(command=AsyncMock(side_effect=lambda: ["ssh", "host"])), app.Notifications()))
            try:
                await self.wait_until(lambda: observations[agent].turn == "stale")
                self.assertEqual(observations[agent].snapshot["response"]["text"], "Done")
                self.assertEqual(observations[agent].error, "connection lost")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
