import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import notifications
from test_dashboard import app


def snapshot(turn, turn_id="one", goal=None, text="Tests passed", kind="final"):
    return {"thread_id": "thread", "turn_id": turn_id, "turn": turn, "goal": goal,
            "response": {"id": "message", "kind": kind, "text": text}}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.agent = app.Agent("agent-3", "user@host.example", "codex")
        self.notifications = notifications.Notifications()

    def test_first_snapshot_is_quiet_and_stop_is_sent_once(self):
        self.notifications.observe(self.agent, snapshot("finished"))
        self.assertTrue(self.notifications.queue.empty())
        self.notifications.observe(self.agent, snapshot("active", "two"))
        self.notifications.observe(self.agent, snapshot("finished", "two"))
        self.notifications.observe(self.agent, snapshot("finished", "two"))
        self.assertEqual(self.notifications.queue.qsize(), 1)
        title, body = self.notifications.queue.get_nowait()
        self.assertEqual(title, "agent-3 — Finished")
        self.assertIn("user@host.example", body)
        self.assertIn("Tests passed", body)

    def test_new_turn_stop_is_detected_even_if_active_poll_was_missed(self):
        self.notifications.observe(self.agent, snapshot("finished"))
        self.notifications.observe(self.agent, snapshot("finished", "two"))
        self.assertEqual(self.notifications.queue.qsize(), 1)

    def test_poll_errors_and_reconnection_do_not_repeat_stopped_notice(self):
        self.notifications.observe(self.agent, {"error": "connection refused"})
        self.notifications.observe(self.agent, snapshot("active"))
        self.notifications.observe(self.agent, {"error": "connection lost"})
        self.notifications.observe(self.agent, snapshot("finished"))
        self.notifications.observe(self.agent, {"error": "connection lost"})
        self.notifications.observe(self.agent, snapshot("finished"))
        self.assertEqual(self.notifications.queue.qsize(), 1)

    def test_approval_can_notify_again_after_work_resumes(self):
        self.notifications.observe(self.agent, snapshot("active"))
        for _ in range(2):
            self.notifications.observe(self.agent, snapshot("approval", kind="progress"))
            self.notifications.observe(self.agent, snapshot("approval", kind="progress"))
            self.notifications.observe(self.agent, snapshot("active"))
        self.assertEqual(self.notifications.queue.qsize(), 2)
        title, body = self.notifications.queue.get_nowait()
        self.assertIn("Approval required", title)
        self.assertIn("Last progress:", body)

    def test_goal_iterations_are_quiet_until_goal_stops(self):
        for goal in ("complete", "blocked", "paused", "usage_limited", "budget_limited"):
            with self.subTest(goal=goal):
                notices = notifications.Notifications()
                notices.observe(self.agent, snapshot("active", goal="active"))
                notices.observe(self.agent, snapshot("finished", goal="active"))
                notices.observe(self.agent, snapshot("finished", "two", goal="active"))
                self.assertTrue(notices.queue.empty())
                notices.observe(self.agent, snapshot("finished", "two", goal=goal))
                self.assertEqual(notices.queue.qsize(), 1)

    def test_failure_and_interruption_notify_even_with_active_goal(self):
        for turn in ("failed", "interrupted"):
            with self.subTest(turn=turn):
                notices = notifications.Notifications()
                notices.observe(self.agent, snapshot("active", goal="active"))
                notices.observe(self.agent, snapshot(turn, goal="active"))
                self.assertIn(turn.capitalize(), notices.queue.get_nowait()[0])

    def test_process_exit_uses_last_known_response(self):
        self.notifications.observe(self.agent, snapshot("active", kind="progress", text="Running checks"))
        self.notifications.observe(self.agent, {"thread_id": None, "turn_id": None, "turn": "absent", "response": None})
        title, body = self.notifications.queue.get_nowait()
        self.assertIn("Stopped", title)
        self.assertIn("Last progress: Running checks", body)

    def test_body_is_short_single_line_snippet_with_escaped_markup(self):
        self.notifications.observe(self.agent, snapshot("active"))
        self.notifications.observe(self.agent, snapshot("finished", text="<b>Safe</b> & ready\n\n" + "word " * 100))
        _, body = self.notifications.queue.get_nowait()
        self.assertIn("&lt;b&gt;Safe&lt;/b&gt; &amp; ready", body)
        self.assertEqual(body.count("\n"), 1)
        self.assertTrue(body.endswith("…"))
        self.assertLess(len(body), 320)


class NotificationWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_failure_does_not_stop_later_notifications(self):
        notices = notifications.Notifications()
        notices.queue.put_nowait(("one", "first"))
        notices.queue.put_nowait(("two", "second"))
        with patch.object(notifications, "capture", side_effect=[FileNotFoundError("notify-send missing"), b""]) as send:
            task = asyncio.create_task(notices.run())
            try:
                await asyncio.wait_for(notices.queue.join(), 2)
                self.assertEqual(send.await_count, 2)
                self.assertIn("notify-send missing", notices.error)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_reader_delivers_notification_without_dashboard_drawing(self):
        agent = app.Agent("test", "host", "codex")
        observations = {agent: app.Observation()}
        notices = notifications.Notifications()
        source = "import asyncio\n"
        for turn in ("active", "finished"):
            source += f"print({json.dumps(json.dumps({'codex': snapshot(turn)}))}, flush=True)\n"
        source += "asyncio.run(asyncio.sleep(60))\n"
        original = asyncio.create_subprocess_exec
        delivered = asyncio.Event()

        async def local_reader(*command, **kwargs):
            return await original(sys.executable, "-u", "-", **kwargs)

        async def send(command):
            self.assertEqual(command[0], "notify-send")
            self.assertIn("Finished", command[-2])
            self.assertIn("Tests passed", command[-1])
            delivered.set()

        with patch.object(app.asyncio, "create_subprocess_exec", side_effect=local_reader), \
             patch.object(notifications, "capture", side_effect=send):
            tasks = [asyncio.create_task(notices.run()), asyncio.create_task(app.watch_host(
                [agent], observations, source.encode(),
                AsyncMock(command=AsyncMock(return_value=["ssh", "host"])), notices))]
            try:
                await asyncio.wait_for(delivered.wait(), 3)
                self.assertEqual(observations[agent].turn, "finished")
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
