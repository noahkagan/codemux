import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import prompt_sender
from test_dashboard import app


class RemoteSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = Path('/custom/codex home')
        self.process = Mock(returncode=0)
        self.process.communicate = AsyncMock(return_value=(b'queued receipt\n', b''))
        self.process.wait = AsyncMock()
        self.spawn = self.enterContext(patch.object(
            prompt_sender.asyncio, 'create_subprocess_exec', new_callable=AsyncMock,
            return_value=self.process))
        self.tmux = self.enterContext(patch.object(
            prompt_sender.status_reader, 'tmux', new_callable=AsyncMock,
            return_value='work-extra\t10\nwork\t20\nwork\t30\n'))
        self.enterContext(patch.object(prompt_sender.status_reader, 'processes', return_value={
            10: (1, 'bash'), 11: (10, 'codex'),
            20: (1, 'bash'), 21: (20, 'codex'),
            30: (1, 'bash'), 31: (30, 'codex'),
        }))
        self.roots = self.enterContext(patch.object(
            prompt_sender.status_reader, 'root_threads',
            side_effect=lambda pid: {(self.home, 'root')} if pid == 21 else set()))

    async def test_exact_session_unique_root_home_and_literal_prompt(self):
        message = "First line\n'quoted' $(touch /tmp/unwanted); `false` 雪\n"
        result = await prompt_sender.send('work', message)
        self.assertEqual([call.args[0] for call in self.roots.call_args_list], [21, 31])
        self.assertEqual(self.spawn.call_args.args,
                         ('codex', 'queue', '--thread', 'root', '--message', message))
        self.assertEqual(self.spawn.call_args.kwargs['env']['CODEX_HOME'], str(self.home))
        self.assertEqual(result, {'session': 'work', 'thread_id': 'root',
                                  'status': 'queued', 'receipt': 'queued receipt'})

    async def test_multiple_root_threads_are_rejected_before_submission(self):
        self.roots.side_effect = lambda pid: {(self.home, str(pid))}
        with self.assertRaisesRegex(ValueError, 'found 2'):
            await prompt_sender.send('work', 'hello')
        self.spawn.assert_not_awaited()

    async def test_no_running_codex_or_no_discoverable_root_is_rejected(self):
        for inventory, error in (({}, 'no running Codex'),
                                 ({20: (1, 'bash'), 22: (20, 'codex')}, 'found 0')):
            with self.subTest(inventory=inventory), patch.object(
                    prompt_sender.status_reader, 'processes', return_value=inventory):
                with self.assertRaisesRegex(ValueError, error):
                    await prompt_sender.send('work', 'hello')
        self.spawn.assert_not_awaited()

    async def test_missing_session_does_not_match_prefix(self):
        with self.assertRaisesRegex(ValueError, 'session not found'):
            await prompt_sender.send('wor', 'hello')
        self.spawn.assert_not_awaited()

    async def test_empty_and_nul_prompts_fail_before_discovery(self):
        for message in ('', ' \n', 'hello\0world'):
            with self.subTest(message=message), self.assertRaises(ValueError):
                await prompt_sender.send('work', message)
        self.tmux.assert_not_awaited()

    async def test_queue_error_is_reported(self):
        self.process.returncode = 1
        self.process.communicate.return_value = (b'', b'queue unavailable\n')
        with self.assertRaisesRegex(RuntimeError, 'queue unavailable'):
            await prompt_sender.send('work', 'hello')

    async def test_timeout_kills_process_and_warns_delivery_is_unknown(self):
        self.process.communicate.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(RuntimeError, 'delivery is unknown'):
            await prompt_sender.send('work', 'hello')
        self.process.kill.assert_called_once()
        self.process.wait.assert_awaited_once()


class SendCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'agents.json'
        self.entry = {'name': 'agent', 'host': 'host.example', 'session': 'work'}
        self.config.write_text(json.dumps([self.entry]))
        self.receipt = {'agent': 'agent', 'session': 'work', 'thread_id': 'root',
                        'status': 'queued', 'receipt': 'accepted'}
        self.sender = self.enterContext(patch.object(app, 'send_prompt', new_callable=AsyncMock,
                                                    return_value=self.receipt))
        self.stdout = self.enterContext(patch.object(app.sys, 'stdout', new_callable=io.StringIO))
        self.stderr = self.enterContext(patch.object(app.sys, 'stderr', new_callable=io.StringIO))
        self.stdin = self.enterContext(patch.object(app.sys, 'stdin', new_callable=io.StringIO))
        self.dashboard = self.enterContext(patch.object(app.curses, 'wrapper'))

    def run_command(self, arguments):
        with patch.object(app.sys, 'argv', ['codemux', '--config', str(self.config), 'queue', *arguments]):
            app.main()

    def test_message_without_tty_prints_receipt(self):
        message = 'hello\nsecond line'
        self.run_command(['agent', '--message', message])
        self.sender.assert_awaited_once_with(app.Agent(**self.entry), message, "queue")
        self.assertEqual(json.loads(self.stdout.getvalue()), self.receipt)
        self.dashboard.assert_not_called()

    def test_utf8_file_preserves_multiline_prompt(self):
        message = '雪\nsecond line\n'
        path = self.root / 'prompt.txt'
        path.write_text(message, encoding='utf-8')
        self.run_command(['agent', '--file', str(path)])
        self.sender.assert_awaited_once_with(app.Agent(**self.entry), message, "queue")

    def test_stdin_preserves_multiline_prompt(self):
        message = 'hello\nsecond line\n'
        self.stdin.write(message)
        self.stdin.seek(0)
        self.run_command(['agent', '--file', '-'])
        self.sender.assert_awaited_once_with(app.Agent(**self.entry), message, "queue")

    def test_unknown_and_ambiguous_agent_names_fail_before_sending(self):
        for entries, target in (([self.entry], 'agen'),
                                ([self.entry, dict(self.entry, session='other')], 'agent')):
            with self.subTest(entries=entries):
                self.config.write_text(json.dumps(entries))
                with self.assertRaises(SystemExit) as error:
                    self.run_command([target, '--message', 'hello'])
                self.assertEqual(error.exception.code, 2)
        self.sender.assert_not_awaited()
        self.assertIn('exactly one', self.stderr.getvalue())

    def test_missing_or_conflicting_prompt_arguments_fail(self):
        for arguments in ([], ['--message', 'hello', '--file', '-']):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                self.run_command(['agent', *arguments])
            self.assertEqual(error.exception.code, 2)
        self.sender.assert_not_awaited()

    def test_empty_and_nul_prompts_fail(self):
        for message in ('', '\n ', 'hello\0world'):
            with self.subTest(message=message), self.assertRaises(SystemExit) as error:
                self.run_command(['agent', '--message', message])
            self.assertEqual(error.exception.code, 2)
        self.sender.assert_not_awaited()

    def test_submission_failure_has_nonzero_exit_and_no_receipt(self):
        self.sender.side_effect = RuntimeError('delivery is unknown')
        with self.assertRaises(SystemExit) as error:
            self.run_command(['agent', '--message', 'hello'])
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.stdout.getvalue(), '')
        self.assertIn('delivery is unknown', self.stderr.getvalue())

    def test_steer_routes_explicit_mode(self):
        with patch.object(app.sys, 'argv', ['codemux', '--config', str(self.config),
                                          'steer', 'agent', '--message', 'hello']):
            app.main()
        self.sender.assert_awaited_once_with(app.Agent(**self.entry), 'hello', 'steer')

    def test_send_is_removed(self):
        with patch.object(app.sys, 'argv', ['codemux', '--config', str(self.config),
                                          'send', 'agent', '--message', 'hello']):
            with self.assertRaises(SystemExit) as error:
                app.main()
        self.assertEqual(error.exception.code, 2)
        self.sender.assert_not_awaited()

    def test_read_requires_no_message_and_prints_snapshot(self):
        snapshot = {'agent': 'agent', 'session': 'work', 'turn': 'finished',
                    'response': {'text': 'Done', 'kind': 'final'}}
        self.sender.return_value = snapshot
        with patch.object(app.sys, 'argv', ['codemux', '--config', str(self.config), 'read', 'agent']):
            app.main()
        self.sender.assert_awaited_once_with(app.Agent(**self.entry), None, 'read')
        self.assertEqual(json.loads(self.stdout.getvalue()), snapshot)
        self.dashboard.assert_not_called()


class SendTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = app.Agent('agent', 'host.example', 'work')
        self.connections = self.enterContext(patch.object(app, 'Connections'))
        self.connections.return_value.__aenter__.return_value.__getitem__.return_value.command = AsyncMock(
            return_value=['ssh', 'host.example'])
        self.process = Mock(returncode=0)
        self.receipt = {'session': 'work', 'thread_id': 'root', 'status': 'queued', 'receipt': 'accepted'}
        self.process.communicate = AsyncMock(return_value=(json.dumps(self.receipt).encode(), b''))
        self.spawn = self.enterContext(patch.object(app.asyncio, 'create_subprocess_exec',
                                                   new_callable=AsyncMock, return_value=self.process))
        self.stop = self.enterContext(patch.object(app, 'stop_process', new_callable=AsyncMock))

    async def test_prompt_transmitted_as_json_stdin_and_receipt_includes_agent(self):
        message = 'hello\n$(false) 雪'
        result = await app.send_prompt(self.agent, message)
        payload = json.loads(self.process.communicate.call_args.args[0])
        self.assertEqual(payload['message'], message)
        self.assertEqual(payload['session'], 'work')
        self.assertEqual(set(payload['sources']), {'status_reader', 'prompt_sender'})
        self.assertNotIn(message, self.spawn.call_args.args[-1])
        self.assertEqual(result, {'agent': 'agent', **self.receipt})
        self.stop.assert_awaited_once_with(self.process)

    async def test_wrong_session_receipt_is_rejected(self):
        self.receipt['session'] = 'other'
        self.process.communicate.return_value = (json.dumps(self.receipt).encode(), b'')
        with self.assertRaisesRegex(RuntimeError, 'Invalid submission receipt'):
            await app.send_prompt(self.agent, 'hello')
        self.stop.assert_awaited_once_with(self.process)

    async def test_malformed_receipts_report_uncertain_delivery(self):
        for output in (b"null", b"[]", b"not-json", b"\xff"):
            with self.subTest(output=output):
                self.process.communicate.return_value = (output, b"")
                with self.assertRaisesRegex(RuntimeError, "delivery is unknown"):
                    await app.send_prompt(self.agent, "hello")

    async def test_transport_timeout_cleans_up_and_reports_uncertain_delivery(self):
        self.process.communicate.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(RuntimeError, 'delivery is unknown'):
            await app.send_prompt(self.agent, 'hello')
        self.stop.assert_awaited_once_with(self.process)

    async def test_read_uses_only_existing_reader_and_validates_snapshot(self):
        snapshot = {'session': 'work', 'turn': 'active', 'response': None}
        self.process.communicate.return_value = (json.dumps(snapshot).encode(), b'')
        result = await app.send_prompt(self.agent, None, 'read')
        payload = json.loads(self.process.communicate.call_args.args[0])
        self.assertEqual(set(payload['sources']), {'status_reader'})
        self.assertEqual(payload['kind'], 'codex')
        self.assertEqual(result, {'agent': 'agent', **snapshot})

    async def test_read_timeout_does_not_claim_delivery(self):
        self.process.communicate.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(RuntimeError, 'Status read timed out'):
            await app.send_prompt(self.agent, None, 'read')
        self.stop.assert_awaited_once()


class SteerProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, turns, reject=False):
        import sys
        import types
        requests = []
        responses = asyncio.Queue()

        class Connection:
            async def send(self, raw):
                request = json.loads(raw)
                requests.append(request)
                method = request["method"]
                if "id" not in request:
                    return
                result = {}
                if method == "thread/read":
                    result = {"thread": {"turns": turns}}
                elif method == "turn/steer":
                    result = {"turnId": "active"}
                elif method == "turn/start":
                    result = {"turn": {"id": "new"}}
                response = {"id": request["id"], "result": result}
                if reject and method == "turn/steer":
                    response = {"id": request["id"], "error": {"message": "turn changed"}}
                await responses.put(json.dumps({"method": "unrelated/event", "params": {}}))
                await responses.put(json.dumps(response))

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await responses.get()

        context = Mock()
        context.__aenter__ = AsyncMock(return_value=Connection())
        context.__aexit__ = AsyncMock(return_value=False)
        connect = Mock(return_value=context)
        modules = {name: types.ModuleType(name) for name in
                   ("websockets", "websockets.asyncio", "websockets.asyncio.client")}
        modules["websockets.asyncio.client"].unix_connect = connect
        self.requests = requests
        with patch.dict(sys.modules, modules):
            result = await prompt_sender.steer("root", "literal \n text", {"CODEX_HOME": "/custom/home"})
        self.assertEqual(connect.call_args.args[0], "/custom/home/app-server-control/app-server-control.sock")
        return result

    async def test_idle_starts_turn_without_resume_or_interrupt(self):
        result = await self.invoke([{"id": "old", "status": "completed"}])
        self.assertEqual(result, {"status": "started", "turn_id": "new"})
        self.assertEqual([r["method"] for r in self.requests],
                         ["initialize", "initialized", "thread/read", "turn/start"])

    async def test_active_steers_exact_turn_and_preserves_literal_input(self):
        result = await self.invoke([{"id": "active", "status": "inProgress"}])
        self.assertEqual(result, {"status": "steered", "turn_id": "active"})
        self.assertEqual(self.requests[-1]["params"], {
            "threadId": "root", "expectedTurnId": "active",
            "input": [{"type": "text", "text": "literal \n text"}]})

    async def test_changed_turn_fails_without_starting_or_retrying(self):
        with self.assertRaisesRegex(RuntimeError, "turn changed"):
            await self.invoke([{"id": "active", "status": "inProgress"}], reject=True)
        self.assertEqual(len(self.requests), 4)
