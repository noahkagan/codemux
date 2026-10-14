"""App-owned OpenSSH masters shared by status, terminals, and forwards."""

import asyncio
from pathlib import Path
import re
import shlex
import tempfile


def diagnostic(error, fallback):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", error.decode(errors="replace"))
    lines = text.strip().splitlines()
    return lines[-1] if lines else fallback


async def stop_process(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def capture(command):
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout=15)
        if process.returncode:
            raise RuntimeError(diagnostic(error, f"{Path(command[0]).name} exited with code {process.returncode}"))
        return output
    finally:
        await stop_process(process)


async def teleport_config(proxy, path):
    output = await capture(["tsh", "config", f"--proxy={proxy}"])
    clusters = set()
    for line in output.decode().splitlines():
        parts = shlex.split(line, comments=True)
        if parts and parts[0].lower() == "proxycommand":
            clusters.update(part.removeprefix("--cluster=") for part in parts if part.startswith("--cluster="))
    if len(clusters) != 1:
        raise RuntimeError("Expected one Teleport cluster in tsh config output")
    path.write_bytes(output)
    return path, clusters.pop()


class Connection:
    def __init__(self, host, socket_path, config=None):
        self.host = host
        self.socket_path = socket_path
        self.config = config
        self.master = None
        self.lock = asyncio.Lock()
        self.base = []
        self.target = host

    async def command(self, *options, interactive=False):
        async with self.lock:
            if self.master is None or self.master.done():
                await self.close()
                self.base = ["ssh", "-S", str(self.socket_path),
                             "-o", "ControlMaster=no", "-o", "ControlPersist=no",
                             "-o", "ForkAfterAuthentication=no", "-o", "BatchMode=yes",
                             "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                             "-o", "ServerAliveCountMax=2", "-o", "ExitOnForwardFailure=yes"]
                if self.config:
                    path, cluster = await asyncio.shield(self.config)
                    self.base += ["-F", str(path)]
                    self.target = self.host if self.host.endswith("." + cluster) else self.host + "." + cluster
                self.socket_path.unlink(missing_ok=True)
                # OpenSSH reads passwords from the controlling terminal, then
                # detaches after authentication. Never pass credentials through Python.
                with tempfile.TemporaryFile() as errors:
                    process = await asyncio.create_subprocess_exec(
                        "ssh", "-o", f"BatchMode={'no' if interactive else 'yes'}",
                        "-f", *self.base[1:], "-M", "-N", self.target,
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                        stderr=None if interactive else errors, start_new_session=not interactive)
                    try:
                        if interactive:
                            code = await process.wait()
                        else:
                            code = await asyncio.wait_for(process.wait(), timeout=15)
                        if code:
                            errors.seek(0)
                            raise RuntimeError(diagnostic(errors.read(), f"SSH authentication failed or canceled (exit {code})"))
                        await capture(self.base + ["-O", "check", self.target])
                    except BaseException:
                        await stop_process(process)
                        await self.stop_master()
                        raise
                self.master = asyncio.create_task(self.monitor())
        # If the master disappears, fail instead of opening an unshared connection.
        return self.base + ["-o", "ProxyCommand=false"] + list(options) + [self.target]

    async def monitor(self):
        try:
            while True:
                await asyncio.sleep(1)
                await capture(self.base + ["-O", "check", self.target])
        except (OSError, RuntimeError, asyncio.TimeoutError):
            pass
        finally:
            await self.stop_master()

    async def stop_master(self):
        if self.socket_path.exists():
            try:
                await capture(self.base + ["-O", "exit", self.target])
            except (OSError, RuntimeError, asyncio.TimeoutError):
                pass

    async def cancel_forward(self, mapping, owner):
        # Never reconnect just to cancel a forward belonging to a dead master.
        async with self.lock:
            if owner is self.master and not owner.done():
                await capture(self.base + ["-O", "cancel", "-L", mapping, self.target])

    async def forward(self, mapping, *, interactive=False):
        await self.command(interactive=interactive)
        async with self.lock:
            if self.master.done():
                raise RuntimeError("SSH disconnected; retry forwarding")
            await capture(self.base + ["-O", "forward", "-L", mapping, self.target])
            return self.master

    async def close(self):
        if self.master:
            self.master.cancel()
            await asyncio.gather(self.master, return_exceptions=True)
            # A newly created monitor may be canceled before entering its finally.
            await self.stop_master()


class Connections:
    def __init__(self, agents):
        self.agents = agents

    async def __aenter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="codemux-")
        root = Path(self.directory.name)
        self.configs = {
            proxy: asyncio.create_task(teleport_config(proxy, root / f"config-{index}"))
            for index, proxy in enumerate(dict.fromkeys(agent.proxy for agent in self.agents if agent.proxy))
        }
        self.hosts = {}
        for agent in self.agents:
            key = (agent.host, agent.proxy)
            if key not in self.hosts:
                self.hosts[key] = Connection(agent.host, root / f"socket-{len(self.hosts)}", self.configs.get(agent.proxy))
        return {agent: self.hosts[agent.host, agent.proxy] for agent in self.agents}

    async def __aexit__(self, *exc):
        try:
            await asyncio.gather(*(connection.close() for connection in self.hosts.values()))
        finally:
            for task in self.configs.values():
                task.cancel()
            await asyncio.gather(*self.configs.values(), return_exceptions=True)
            self.directory.cleanup()
