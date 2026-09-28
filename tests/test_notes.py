import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_dashboard import app, Screen


class NoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "agents.json"
        self.entries = [
            {"name": "one", "host": "host.example", "session": "one"},
            {"name": "two", "host": "host.example", "session": "two", "note": "Existing work"},
        ]
        self.path.write_text(json.dumps(self.entries))
        self.agents = [app.Agent(**entry) for entry in self.entries]
        self.dashboard = app.Dashboard(Screen(), self.agents, self.path)

    async def type_note(self, text):
        await self.dashboard.handle("n")
        for character in text:
            await self.dashboard.handle(character)

    async def test_save_survives_reload_and_preserves_agent_state(self):
        agent = self.dashboard.agent
        observation = self.dashboard.observations[agent]
        self.dashboard.expanded.add(agent)
        self.dashboard.forwards[agent] = object()
        await self.type_note("Check j/k/q/a ć GPU work")
        self.assertEqual(self.dashboard.selected, 0)
        await self.dashboard.handle("\n")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved[0]["note"], "Check j/k/q/a ć GPU work")
        self.assertEqual(saved[1], self.entries[1])
        self.assertIs(self.dashboard.observations[self.dashboard.agent], observation)
        self.assertIn(self.dashboard.agent, self.dashboard.expanded)
        self.assertIn(self.dashboard.agent, self.dashboard.forwards)
        restarted = app.Dashboard(Screen(), [app.Agent(**entry) for entry in saved], self.path)
        _, rows, _ = restarted.table_rows(80)
        self.assertIn("Note: Check j/k/q/a ć GPU work", "\n".join(text for text, _ in rows))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_cancel_and_clear(self):
        await self.dashboard.handle("j")
        original = self.path.read_bytes()
        await self.type_note(" additional")
        await self.dashboard.handle("\x1b")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.dashboard.agent.note, "Existing work")
        await self.dashboard.handle("n")
        self.assertEqual(self.dashboard.note_input, "Existing work")
        await self.dashboard.handle("\x15")
        await self.dashboard.handle("\n")
        self.assertNotIn("note", json.loads(self.path.read_text())[1])
        self.assertEqual(self.dashboard.agent.note, "")

    async def test_backspace_and_special_keys(self):
        await self.type_note("draft")
        await self.dashboard.handle(curses.KEY_LEFT)
        await self.dashboard.handle(curses.KEY_BACKSPACE)
        self.assertEqual(self.dashboard.note_input, "draf")
        await self.dashboard.handle("\n")
        self.assertEqual(self.dashboard.agent.note, "draf")

    async def test_failed_write_preserves_config_and_draft_for_retry(self):
        original = self.path.read_bytes()
        await self.type_note("Draft")
        with patch.object(app.Path, "replace", side_effect=OSError("Disk full")):
            await self.dashboard.handle("\n")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.dashboard.agent.note, "")
        self.assertEqual(self.dashboard.note_input, "Draft")
        self.assertIn("Disk full", self.dashboard.message)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        await self.dashboard.handle("\n")
        self.assertEqual(self.dashboard.agent.note, "Draft")

    async def test_preserves_other_config_edits_and_refuses_changed_agent(self):
        await self.type_note("Draft")
        self.entries[1]["web_port"] = 9000
        self.path.write_text(json.dumps(self.entries))
        await self.dashboard.handle("\n")
        self.assertEqual(json.loads(self.path.read_text())[1]["web_port"], 9000)
        await self.dashboard.handle("n")
        self.entries[0]["session"] = "replacement"
        self.path.write_text(json.dumps(self.entries))
        original = self.path.read_bytes()
        await self.dashboard.handle("\n")
        self.assertIn("configuration changed", self.dashboard.message)
        self.assertEqual(self.path.read_bytes(), original)
