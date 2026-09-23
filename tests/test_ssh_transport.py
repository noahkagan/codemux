import asyncio
from collections import namedtuple
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

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
        source = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
        process = await self.original_spawn(sys.executable, "-c", source, socket_path, **kwargs)
        self.spawned.append(process)
        return process

    async def test_concurrent_requests_share_master_and_reconnect_after_exit(self):
        with patch.object(ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
            commands = await asyncio.gather(*(self.connection.command() for _ in range(3)))
            self.assertEqual(len(self.spawned), 1)
            self.assertEqual(commands[0], commands[1])
            self.assertIn("ProxyCommand=false", commands[0])
            self.spawned[0].kill()
            await self.spawned[0].wait()
            await self.connection.command("-t")
            self.assertEqual(len(self.spawned), 2)
            await self.connection.close()
            self.assertTrue(all(process.returncode is not None for process in self.spawned))

    async def test_cancel_only_removes_forward_from_its_original_master(self):
        with patch.object(ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
            await self.connection.command()
            owner = self.connection.process
            with patch.object(ssh, "capture", new_callable=AsyncMock) as capture:
                await self.connection.cancel_forward("127.0.0.1:1234:127.0.0.1:8000", owner)
                self.assertIn("cancel", capture.call_args.args[0])
                self.assertIsNone(owner.returncode)
                await ssh.stop_process(owner)
                await self.connection.command()
                await self.connection.cancel_forward("127.0.0.1:1234:127.0.0.1:8000", owner)
                self.assertEqual(capture.await_count, 1)

    async def test_master_failure_reports_diagnostic(self):
        async def failed_master(*command, **kwargs):
            return await self.original_spawn(
                sys.executable, "-c", "import sys; sys.stderr.write('certificate expired'); sys.exit(1)", **kwargs)

        with patch.object(ssh.asyncio, "create_subprocess_exec", side_effect=failed_master):
            with self.assertRaisesRegex(RuntimeError, "certificate expired"):
                await self.connection.command()

    async def test_cancel_during_startup_terminates_master(self):
        async def stalled_master(*command, **kwargs):
            process = await self.original_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
            self.spawned.append(process)
            return process

        with patch.object(ssh.asyncio, "create_subprocess_exec", side_effect=stalled_master):
            task = asyncio.create_task(self.connection.command())
            while not self.spawned:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(self.spawned[0].returncode)

    async def test_teleport_target_uses_cluster_in_generated_config(self):
        output = b'Host *.cluster.example\n ProxyCommand "/path to/tsh" proxy ssh --cluster=cluster.example %r@%h:%p\n'
        config_path = Path(self.directory.name) / "config"
        with patch.object(ssh, "capture", new_callable=AsyncMock, return_value=output):
            self.connection.config = asyncio.create_task(ssh.teleport_config("proxy.example", config_path))
            with patch.object(ssh.asyncio, "create_subprocess_exec", side_effect=self.fake_master):
                command = await self.connection.command()
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
