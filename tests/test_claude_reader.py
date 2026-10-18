import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import status_reader as reader

SESSION = "e6c5c3ee-ea70-4f24-b259-4d2ed32dfd55"


def user(content, **extra):
    return {"type": "user", "uuid": "u", "timestamp": "2026-10-18T06:00:00.000Z", "message": {"content": content}, **extra}


def tool_result():
    return user([{"type": "tool_result", "content": "ok"}])


def assistant(text, uuid="a", second=1):
    return {"type": "assistant", "uuid": uuid, "timestamp": f"2026-10-18T06:00:0{second}.000Z",
            "message": {"content": [{"type": "text", "text": text}]}}


def tool_use():
    return {"type": "assistant", "uuid": "t", "timestamp": "2026-10-18T06:00:02.000Z",
            "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]}}


DONE = {"type": "system", "subtype": "turn_duration", "uuid": "d", "timestamp": "2026-10-18T06:00:09.000Z"}
APPROVAL = """
 Create file
 new.txt
 Do you want to create new.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits for this session (shift+tab)
   3. No
 Esc to cancel · Tab to amend
"""


class TranscriptTests(unittest.TestCase):
    def read(self, *rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{SESSION}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return reader.read_transcript(path, SESSION)

    def test_empty_transcript_is_idle(self):
        self.assertEqual(self.read(), {"thread_id": SESSION, "turn_id": None, "turn": "idle", "goal": None, "response": None})

    def test_active_turn_shows_latest_progress_text(self):
        value = self.read(user("go"), assistant("Looking"), tool_use(), tool_result())
        self.assertEqual(value["turn"], "active")
        self.assertEqual(value["response"]["text"], "Looking")
        self.assertEqual(value["response"]["kind"], "progress")
        self.assertEqual(value["response"]["timestamp"], 1792303201.0)

    def test_new_prompt_keeps_previous_final_response_until_new_text(self):
        value = self.read(user("one"), assistant("Answer"), DONE, user("two"))
        self.assertEqual((value["turn"], value["response"]["text"], value["response"]["kind"]), ("active", "Answer", "final"))
        value = self.read(user("one"), assistant("Answer"), DONE, user("two"), assistant("Working"))
        self.assertEqual((value["response"]["text"], value["response"]["kind"]), ("Working", "progress"))

    def test_turn_duration_finishes_with_final_response(self):
        value = self.read(user("go"), assistant("Done"), DONE)
        self.assertEqual((value["turn"], value["response"]["kind"]), ("finished", "final"))

    def test_interrupt_marker_before_turn_duration_is_interrupted(self):
        value = self.read(user("go"), tool_use(), tool_result(),
                          user([{"type": "text", "text": "[Request interrupted by user for tool use]"}]), DONE)
        self.assertEqual(value["turn"], "interrupted")
        self.assertEqual(self.read(*[user("go"), DONE, user("again"), assistant("Ok"), DONE])["turn"], "finished")

    def test_meta_rows_are_ignored(self):
        value = self.read(user("go"), assistant("Done"), DONE, user("caveat", isMeta=True))
        self.assertEqual((value["turn"], value["response"]["text"]), ("finished", "Done"))

    def test_large_transcript_reports_its_latest_turn(self):
        value = self.read(assistant("x" * 2_000_000), user("go"), assistant("Tail"), DONE)
        self.assertEqual((value["turn"], value["response"]["text"]), ("finished", "Tail"))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.proc = self.home / "proc"
        (self.proc / "20" / "fd").mkdir(parents=True)
        project = self.home / "claude" / "projects" / "-work"
        project.mkdir(parents=True)
        self.transcript = project / f"{SESSION}.jsonl"
        self.inventory = {10: (1, "bash"), 20: (10, "claude"), 30: (1, "claude")}
        patcher = patch.multiple(reader, PROC=self.proc, CLAUDE_HOME=self.home / "claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_tasks(self, session=SESSION):
        (self.proc / "20" / "fd" / "30").symlink_to(f"/tmp/claude-1000/-work/{session}/tasks")

    def test_session_maps_from_open_tasks_directory(self):
        self.open_tasks()
        self.transcript.write_text(json.dumps(user("go")) + "\n")
        value = reader.claude_session({10}, self.inventory)
        self.assertEqual((value["thread_id"], value["turn"]), (SESSION, "active"))

    def test_session_maps_from_process_file(self):
        sessions = self.home / "claude" / "sessions"
        sessions.mkdir()
        (sessions / "20.json").write_text(json.dumps({"pid": 20, "sessionId": SESSION}))
        self.transcript.write_text(json.dumps(user("go")) + "\n")
        self.assertEqual(reader.claude_session({10}, self.inventory)["thread_id"], SESSION)

    def test_unmappable_process_is_reported(self):
        with self.assertRaisesRegex(ValueError, "not visible"):
            reader.claude_session({10}, self.inventory)

    def test_no_claude_process_is_absent(self):
        self.assertEqual(reader.claude_session({10}, {10: (1, "bash"), 30: (1, "claude")})["turn"], "absent")

    def test_multiple_processes_are_ambiguous(self):
        with self.assertRaisesRegex(ValueError, "found 2"):
            reader.claude_session({10}, {10: (1, "bash"), 20: (10, "claude"), 21: (10, "claude")})


class ApprovalTests(unittest.TestCase):
    def test_visible_dialog_with_either_selection(self):
        self.assertTrue(reader.claude_approval_visible(APPROVAL))
        self.assertTrue(reader.claude_approval_visible(APPROVAL.replace("❯ 1.", "  1.").replace("  3.", "❯ 3.")))

    def test_scrollback_or_partial_dialog_does_not_report_approval(self):
        self.assertFalse(reader.claude_approval_visible(APPROVAL + "❯ \n"))
        self.assertFalse(reader.claude_approval_visible(APPROVAL.replace("3. No", "3. Maybe")))
        self.assertFalse(reader.claude_approval_visible(""))
