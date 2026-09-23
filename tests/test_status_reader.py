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
                    thread_id TEXT, turn_id TEXT, item_id TEXT, item_json TEXT);
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
            db.execute("INSERT INTO thread_items VALUES ('root','first','reply',?)", (
                json.dumps({"type": "agentMessage", "phase": "final_answer", "text": "Last answer\nSecond line"}),))

    def test_active_turn_keeps_previous_final_response_and_goal(self):
        with sqlite3.connect(self.home / "thread_history_1.sqlite") as db:
            db.execute("INSERT INTO thread_turns VALUES ('root','second',2,'inProgress',NULL,NULL)")
            db.execute("INSERT INTO thread_items VALUES ('root','second','progress',?)", (
                json.dumps({"type": "agentMessage", "phase": "commentary", "text": "Still working"}),))
        before = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in self.home.glob("*.sqlite")}
        value = reader.read_thread(self.home, "root")
        self.assertEqual(value["turn"], "active")
        self.assertEqual(value["goal"], "blocked")
        self.assertEqual(value["response"]["text"], "Last answer\nSecond line")
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).digest() for p in self.home.glob("*.sqlite")})

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
        process.communicate = unittest.mock.AsyncMock(return_value=(b"one\t10\ntwo\t20\n", b""))
        with patch.object(reader.asyncio, "create_subprocess_exec", return_value=process), \
             patch.object(reader, "processes", return_value={}), \
             patch.object(reader, "read_session", side_effect=[{"turn": "finished"}, sqlite3.OperationalError("schema changed")]):
            values = await reader.snapshot(["one", "two", "missing"])
        self.assertEqual(values["one"]["turn"], "finished")
        self.assertIn("schema changed", values["two"]["error"])
        self.assertEqual(values["missing"]["error"], "tmux session not found")
