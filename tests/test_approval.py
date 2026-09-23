import unittest
from unittest.mock import AsyncMock, patch

import status_reader as reader
from test_dashboard import app, Screen


APPROVAL = """
  Would you like to run the following command?

  Reason: Start the requested viewer.
  $ example-viewer serve

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `example-viewer` (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel

"""


class ApprovalTests(unittest.TestCase):
    def test_visible_approval_with_either_selection(self):
        self.assertTrue(reader.approval_visible(APPROVAL))
        self.assertTrue(reader.approval_visible(APPROVAL.replace("› 1.", "  1.").replace("  3.", "› 3.")))
        self.assertTrue(reader.approval_visible(APPROVAL[APPROVAL.index("› 1."):]))

    def test_old_prompt_or_partial_match_does_not_report_approval(self):
        self.assertFalse(reader.approval_visible(APPROVAL + "› Ask Codex to do anything\n"))
        self.assertFalse(reader.approval_visible("Would you like to run the following command?"))
        self.assertFalse(reader.approval_visible("Press enter to confirm or esc to cancel"))
        self.assertFalse(reader.approval_visible(APPROVAL.replace("Yes, proceed", "Use default settings")))

    def test_approval_is_yellow_even_when_goal_is_active(self):
        agent = app.Agent("test", "host", "codex")
        dashboard = app.Dashboard(Screen(), [agent])
        dashboard.colors = {"attention": 256, "running": 512}
        dashboard.observations[agent].update({"turn": "approval", "goal": "active", "response": None})
        self.assertEqual(dashboard.row_style(dashboard.observations[agent]), 256)


class ApprovalSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_clears_next_poll_and_ignores_unrelated_panes(self):
        inventory = {10: (1, "bash"), 11: (10, "codex"), 20: (1, "bash")}
        pane_list = "codex\t20\t%1\ncodex\t10\t%0\n"
        tmux = AsyncMock(side_effect=[pane_list, APPROVAL, pane_list, "Working (esc to interrupt)", pane_list])
        with patch.object(reader, "tmux", tmux), patch.object(reader, "processes", return_value=inventory), \
             patch.object(reader, "read_session", side_effect=[
                 {"turn": "active", "response": {"text": "Starting viewer"}},
                 {"turn": "active", "response": {"text": "Starting viewer"}},
                 {"turn": "finished", "response": {"text": "Done"}},
             ]):
            value = (await reader.snapshot(["codex"]))["codex"]
            self.assertEqual(value["turn"], "approval")
            self.assertEqual(value["response"]["text"], "Starting viewer")
            self.assertEqual((await reader.snapshot(["codex"]))["codex"]["turn"], "active")
            self.assertEqual((await reader.snapshot(["codex"]))["codex"]["turn"], "finished")
        captures = [call.args for call in tmux.await_args_list if call.args[0] == "capture-pane"]
        self.assertEqual(captures, [("capture-pane", "-p", "-J", "-t", "%0")] * 2)

    async def test_capture_failure_is_unavailable_without_hiding_healthy_session(self):
        tmux = AsyncMock(side_effect=["one\t10\t%0\ntwo\t20\t%1\n", RuntimeError("pane disappeared")])
        inventory = {10: (1, "bash"), 11: (10, "codex")}
        with patch.object(reader, "tmux", tmux), patch.object(reader, "processes", return_value=inventory), \
             patch.object(reader, "read_session", side_effect=[{"turn": "active"}, {"turn": "finished"}]):
            values = await reader.snapshot(["one", "two"])
        self.assertEqual(values["one"], {"error": "pane disappeared"})
        self.assertEqual(values["two"]["turn"], "finished")
