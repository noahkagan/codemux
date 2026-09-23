import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import status_reader as reader


class StatusReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        for name, schema in {
            "state_5.sqlite": "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT)",
            "goals_1.sqlite": "CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, status TEXT)",
            "thread_history_1.sqlite": """
                CREATE TABLE thread_turns (
                    thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
                    status TEXT, final_agent_item_id TEXT, completed_at INTEGER);
                CREATE TABLE thread_items (
                    thread_id TEXT, turn_id TEXT, item_id TEXT, item_json TEXT,
                    rollout_ordinal INTEGER, created_at_ms INTEGER, item_type TEXT);
            """,
        }.items():
            with sqlite3.connect(self.home / name) as db:
                db.executescript(schema)
        with sqlite3.connect(self.home / "state_5.sqlite") as db:
            db.executemany("INSERT INTO threads VALUES (?,?)", [("root", "cli"), ("child", '{"subagent":{}}')])
        with sqlite3.connect(self.home / "goals_1.sqlite") as db:
            db.execute("INSERT INTO thread_goals VALUES ('root','blocked')")
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("INSERT INTO thread_turns VALUES ('root','first',1,'completed','reply',100)")
            db.execute("INSERT INTO thread_items VALUES ('root','first','reply',?,1,99000,'agentMessage')", (
                json.dumps({"type": "agentMessage", "phase": "final_answer", "text": "Last answer\nSecond line"}),))

    def test_active_turn_shows_latest_progress_without_changing_databases(self):
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("INSERT INTO thread_turns VALUES ('root','second',2,'inProgress',NULL,NULL)")
            db.execute("INSERT INTO thread_items VALUES ('root','second','progress',?,3,200000,'agentMessage')", (
                json.dumps({"type": "agentMessage", "phase": "commentary", "text": "Still working"}),))
            # Insertion order, tool output, other turns, and subagents must not determine the preview.
            for thread, turn, ordinal, item_type in [
                ("root", "second", 2, "agentMessage"), ("root", "second", 4, "commandExecution"),
                ("root", "second", 5, "reasoning"), ("child", "second", 6, "agentMessage"),
                ("root", "first", 7, "agentMessage"),
            ]:
                db.execute("INSERT INTO thread_items VALUES (?,?,?, ?,?,210000,?)", (
                    thread, turn, f"other-{ordinal}", json.dumps({"type": item_type, "text": "Do not show"}), ordinal, item_type))
        before = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in self.home.glob("*.sqlite")}
        value = reader.read_thread(self.home, "root")
        self.assertEqual(value["turn"], "active")
        self.assertEqual(value["goal"], "blocked")
        self.assertEqual(value["response"], {"id": "progress", "text": "Still working", "kind": "progress", "timestamp": 200})
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).digest() for p in self.home.glob("*.sqlite")})

    def test_preview_updates_then_switches_to_final_answer(self):
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("INSERT INTO thread_turns VALUES ('root','second',2,'inProgress',NULL,NULL)")
        self.assertEqual(reader.read_thread(self.home, "root")["response"]["text"], "Last answer\nSecond line")
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("INSERT INTO thread_items VALUES ('root','second','progress',?,2,200000,'agentMessage')", (
                json.dumps({"type": "agentMessage", "phase": "commentary", "text": "Working"}),))
        self.assertEqual(reader.read_thread(self.home, "root")["response"]["text"], "Working")
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("UPDATE thread_items SET item_json=? WHERE item_id='progress'", (
                json.dumps({"type": "agentMessage", "phase": "commentary", "text": "Working on tests"}),))
        self.assertEqual(reader.read_thread(self.home, "root")["response"]["text"], "Working on tests")
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("INSERT INTO thread_items VALUES ('root','second','done',?,3,300000,'agentMessage')", (
                json.dumps({"type": "agentMessage", "phase": "final_answer", "text": "Done"}),))
        self.assertEqual(reader.read_thread(self.home, "root")["response"]["kind"], "final")
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("UPDATE thread_turns SET status='completed', final_agent_item_id='done', completed_at=301 WHERE turn_id='second'")
        value = reader.read_thread(self.home, "root")
        self.assertEqual(value["turn"], "finished")
        self.assertEqual(value["response"], {"id": "done", "text": "Done", "kind": "final", "timestamp": 301})

    def test_first_active_turn_without_messages_has_no_response(self):
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("DELETE FROM thread_turns")
            db.execute("DELETE FROM thread_items")
            db.execute("INSERT INTO thread_turns VALUES ('root','first',1,'inProgress',NULL,NULL)")
        value = reader.read_thread(self.home, "root")
        self.assertEqual(value["turn"], "active")
        self.assertIsNone(value["response"])

    def test_finished_turn_and_absent_goal(self):
        with sqlite3.connect(self.home / "goals_1.sqlite") as db:
            db.execute("DELETE FROM thread_goals")
        value = reader.read_thread(self.home, "root")
        self.assertEqual(value["turn"], "finished")
        self.assertIsNone(value["goal"])

    def test_missing_database_is_not_created(self):
        missing = self.home / "missing.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            with reader.database(missing):
                pass
        self.assertFalse(missing.exists())

    def test_unsupported_schema_raises_instead_of_inventing_status(self):
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("ALTER TABLE thread_turns RENAME TO changed_schema")
        with self.assertRaises(sqlite3.OperationalError):
            reader.read_thread(self.home, "root")

    def test_root_mapping_excludes_subagents_and_other_panes(self):
        proc = self.home / "proc"
        fds = proc / "20" / "fd"
        fds.mkdir(parents=True)
        (fds / "1").symlink_to(self.home / "thread-writer-locks" / "child.lock")
        (fds / "2").symlink_to(self.home / "thread-writer-locks" / "root.lock")
        inventory = {10: (1, "bash"), 11: (10, "node"), 20: (11, "codex"), 30: (1, "codex")}
        with patch.object(reader, "PROC", proc):
            value = reader.read_session({10}, inventory)
        self.assertEqual(value["thread_id"], "root")

    def test_multiple_root_threads_are_reported_as_ambiguous(self):
        inventory = {10: (1, "bash"), 20: (10, "codex"), 21: (10, "codex")}
        with patch.object(reader, "root_threads", side_effect=[{(self.home, "root")}, {(self.home, "other")} ]):
            with self.assertRaisesRegex(ValueError, "found 2"):
                reader.read_session({10}, inventory)

    def test_no_codex_process_clears_thread_response_and_goal(self):
        value = reader.read_session({10}, {10: (1, "bash"), 30: (1, "codex")})
        self.assertEqual(value, {"thread_id": None, "turn": "absent", "goal": None, "response": None})


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_session_and_failed_reader_do_not_hide_other_session(self):
        process = unittest.mock.Mock(returncode=0)
        process.communicate = unittest.mock.AsyncMock(return_value=(b"one\t10\t%0\ntwo\t20\t%1\n", b""))
        with patch.object(reader.asyncio, "create_subprocess_exec", return_value=process), \
             patch.object(reader, "processes", return_value={}), \
             patch.object(reader, "read_session", side_effect=[{"turn": "finished"}, sqlite3.OperationalError("schema changed")]):
            values = await reader.snapshot(["one", "two", "missing"])
        self.assertEqual(values["one"]["turn"], "finished")
        self.assertIn("schema changed", values["two"]["error"])
        self.assertEqual(values["missing"]["error"], "tmux session not found")
