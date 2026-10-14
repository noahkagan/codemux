import asyncio
from collections import namedtuple
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import ssh_transport as ssh


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.connection = ssh.Connection("user@host", Path(self.directory.name) / "socket")
        self.spawned = []
        self.original_spawn = asyncio.create_subprocess_exec

    async def asyncTearDown(self):
        await self.connection.close()
        self.directory.cleanup()

    async def fake_master(self, *command, **kwargs):
        socket_path = command[command.index("-S") + 1]
        source = "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()"
        process = await self.original_spawn(sys.executable, "-c", source, socket_path, **kwargs)
        self.spawned.append(process)
        return process

    async def control(self, command):
        operation = command[command.index("-O") + 1]
        if operation == "exit":
            self.connection.socket_path.unlink(missing_ok=True)
        elif not self.connection.socket_path.exists():
            raise RuntimeError("Control socket disconnected")
        return b""

    async def test_concurrent_requests_share_master_and_reconnect_after_exit(self):
        with patch.object(ssh, "capture", side_effect=self.control), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
            commands = await asyncio.gather(*(self.connection.command() for _ in range(3)))
            self.assertEqual(len(self.spawned), 1)
            self.assertEqual(commands[0], commands[1])
            self.assertIn("ProxyCommand=false", commands[0])
            owner = self.connection.master
            self.connection.socket_path.unlink()
            await asyncio.wait_for(asyncio.shield(owner), timeout=3)
            await self.connection.command("-t")
            self.assertEqual(len(self.spawned), 2)
            self.assertIsNot(owner, self.connection.master)
            await self.connection.close()
            self.assertFalse(self.connection.socket_path.exists())
            self.assertTrue(all(process.returncode is not None for process in self.spawned))

    async def test_cancel_only_removes_forward_from_its_original_master(self):
        with patch.object(ssh, "capture", side_effect=self.control) as capture, patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
            await self.connection.command()
            owner = self.connection.master
            mapping = "127.0.0.1:1234:127.0.0.1:8000"
            await self.connection.cancel_forward(mapping, owner)
            cancels = lambda: [call for call in capture.call_args_list if "cancel" in call.args[0]]
            self.assertEqual(len(cancels()), 1)
            self.assertFalse(owner.done())
            await self.connection.close()
            await self.connection.command()
            await self.connection.cancel_forward(mapping, owner)
            self.assertEqual(len(cancels()), 1)
            await self.connection.close()

    async def test_forward_keeps_owner_when_reconnect_waits_for_registration(self):
        registering, registered = asyncio.Event(), asyncio.Event()

        async def control(command):
            result = await self.control(command)
            if "forward" in command:
                registering.set()
                await registered.wait()
            return result

        with patch.object(ssh, "capture", side_effect=control), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
            await self.connection.command()
            owner = self.connection.master
            forward = asyncio.create_task(self.connection.forward("127.0.0.1:1234:127.0.0.1:8000"))
            try:
                await asyncio.wait_for(registering.wait(), timeout=3)
                self.connection.socket_path.unlink()
                await asyncio.wait_for(asyncio.shield(owner), timeout=3)
                reconnect = asyncio.create_task(self.connection.command())
                await asyncio.sleep(0)
                self.assertFalse(reconnect.done())
            finally:
                registered.set()
            result, _ = await asyncio.wait_for(asyncio.gather(forward, reconnect), timeout=3)
            self.assertIs(result, owner)
            self.assertIsNot(result, self.connection.master)
            await self.connection.close()

    async def test_forward_rejects_disconnection_before_registration(self):
        command = self.connection.command

        async def disconnect(**options):
            result = await command(**options)
            await self.connection.close()
            return result

        with patch.object(ssh, "capture", side_effect=self.control) as capture, patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master), patch.object(
                self.connection, "command", side_effect=disconnect):
            with self.assertRaisesRegex(RuntimeError, "SSH disconnected"):
                await self.connection.forward("127.0.0.1:1234:127.0.0.1:8000")
            self.assertFalse(any("forward" in call.args[0] for call in capture.call_args_list))

    async def test_master_failure_reports_diagnostic(self):
        async def failed_master(*command, **kwargs):
            return await self.original_spawn(
                sys.executable, "-c", "import sys; sys.stderr.write('\\x1b[31mERROR: \\x1b[0mssh: cert has expired\\n'); sys.exit(1)", **kwargs)

        with patch.object(ssh, "capture", side_effect=self.control), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=failed_master):
            with self.assertRaisesRegex(RuntimeError, "^ERROR: ssh: cert has expired$"):
                await self.connection.command()
        with patch.object(ssh, "capture", side_effect=self.control), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
            command = await self.connection.command()
            self.assertEqual(command[-1], "user@host")
            self.assertFalse(self.connection.master.done())
            await self.connection.close()

    async def test_only_interactive_startup_allows_authentication_prompts(self):
        with patch.object(ssh, "capture", side_effect=self.control), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master) as spawn:
            await self.connection.command()
            self.assertIn("BatchMode=yes", spawn.call_args.args)
            await self.connection.close()
            with patch.object(ssh.asyncio, "wait_for", wraps=asyncio.wait_for) as wait_for:
                command = await self.connection.command(interactive=True)
            self.assertFalse(any(call.kwargs.get("timeout") == 15 for call in wait_for.call_args_list))
            self.assertIn("BatchMode=no", spawn.call_args.args)
            self.assertIn("-f", spawn.call_args.args)
            self.assertIn("-M", spawn.call_args.args)
            self.assertIn("-N", spawn.call_args.args)
            self.assertFalse(spawn.call_args.kwargs.get("start_new_session", False))
            self.assertIsNone(spawn.call_args.kwargs.get("stderr"))
            self.assertIn("BatchMode=yes", command)
            await self.connection.command(interactive=True)
            self.assertEqual(spawn.call_count, 2)
            await self.connection.close()

    async def test_background_request_waits_for_interactive_authentication(self):
        started, authenticated = asyncio.Event(), asyncio.Event()

        async def authenticate(*command, **kwargs):
            started.set()
            await authenticated.wait()
            return await self.fake_master(*command, **kwargs)

        with patch.object(ssh, "capture", side_effect=self.control), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=authenticate) as spawn:
            interactive = asyncio.create_task(self.connection.command(interactive=True))
            await asyncio.wait_for(started.wait(), timeout=3)
            background = asyncio.create_task(self.connection.command())
            try:
                await asyncio.sleep(0)
                self.assertFalse(background.done())
                self.assertEqual(spawn.call_count, 1)
            finally:
                authenticated.set()
            commands = await asyncio.gather(interactive, background)
            self.assertEqual(commands[0], commands[1])
            self.assertEqual(spawn.call_count, 1)
            await self.connection.close()

    async def test_close_before_monitor_starts_closes_master(self):
        with patch.object(ssh, "capture", side_effect=self.control), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
            await self.connection.command()
            await self.connection.close()
            self.assertTrue(self.connection.master.done())
            self.assertFalse(self.connection.socket_path.exists())

    async def test_capture_strips_ansi_diagnostics(self):
        with self.assertRaisesRegex(RuntimeError, "^ERROR: Active profile expired.$"):
            await ssh.capture([sys.executable, "-c",
                               "import sys; sys.stderr.write('\\x1b[31mERROR: \\x1b[0mActive profile expired.\\n'); sys.exit(1)"])

    async def test_cancel_during_startup_terminates_launcher_and_master(self):
        async def stalled_master(*command, **kwargs):
            source = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
            process = await self.original_spawn(
                sys.executable, "-c", source, str(self.connection.socket_path), **kwargs)
            self.spawned.append(process)
            return process

        with patch.object(ssh, "capture", side_effect=self.control), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=stalled_master):
            for interactive in (False, True):
                with self.subTest(interactive=interactive):
                    task = asyncio.create_task(self.connection.command(interactive=interactive))
                    try:
                        for _ in range(300):
                            if self.connection.socket_path.exists():
                                break
                            await asyncio.sleep(0.01)
                        self.assertTrue(self.connection.socket_path.exists())
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    self.assertIsNotNone(self.spawned[-1].returncode)
                    self.assertFalse(self.connection.socket_path.exists())

    async def test_teleport_target_uses_cluster_in_generated_config(self):
        output = b'Host *.cluster.example\n ProxyCommand "/path to/tsh" proxy ssh --cluster=cluster.example %r@%h:%p\n'
        config_path = Path(self.directory.name) / "config"
        async def capture(command):
            return output if command[0] == "tsh" else await self.control(command)

        with patch.object(ssh, "capture", side_effect=capture), patch.object(
                ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
            self.connection.config = asyncio.create_task(ssh.teleport_config("proxy.example", config_path))
            command = await self.connection.command()
            await self.connection.close()
        self.assertEqual(command[-1], "user@host.cluster.example")
        self.assertEqual(config_path.read_bytes(), output)

    async def test_scope_shares_host_and_proxy_config_and_removes_temporary_files(self):
        # Frozen tuples provide hashable agent identities without depending on the UI module.
        Agent = namedtuple("Agent", "name host proxy")
        agents = [Agent("one", "host", "proxy"), Agent("two", "host", "proxy"), Agent("three", "other", "proxy")]
        configs = []

        async def config(proxy, path):
            configs.append(path)
            path.write_text("config")
            return path, "cluster"

        with patch.object(ssh, "teleport_config", side_effect=config):
            async with ssh.Connections(agents) as connections:
                await asyncio.gather(*(connection.config for connection in connections.values()))
                self.assertIs(connections[agents[0]], connections[agents[1]])
                self.assertIsNot(connections[agents[0]], connections[agents[2]])
                self.assertEqual(len(configs), 1)
                root = configs[0].parent
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertFalse(root.exists())
