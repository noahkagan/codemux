"""App-owned OpenSSH masters shared by status, terminals, and forwards."""

import asyncio
from pathlib import Path
import shlex
import tempfile


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
            lines = error.decode(errors="replace").strip().splitlines()
            raise RuntimeError(lines[-1] if lines else f"SSH exited with code {process.returncode}")
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
        self.process = None
        self.diagnostics = None
        self.lock = asyncio.Lock()
        self.base = []
        self.target = host

    async def command(self, *options):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
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
                self.process = await asyncio.create_subprocess_exec(
                    *self.base, "-M", "-N", self.target,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True)
                self.diagnostics = asyncio.create_task(self.process.stderr.read())
                try:
                    await asyncio.wait_for(self.wait_ready(), timeout=15)
                except BaseException:
                    await self.close()
                    raise
        # If the master disappears, fail instead of opening an unshared connection.
        return self.base + ["-o", "ProxyCommand=false"] + list(options) + [self.target]

    async def wait_ready(self):
        while True:
            if self.process.returncode is not None:
                lines = (await self.diagnostics).decode(errors="replace").strip().splitlines()
                raise RuntimeError(lines[-1] if lines else "SSH connection closed")
            if self.socket_path.exists():
                return
            await asyncio.sleep(0.05)

    async def cancel_forward(self, mapping, owner):
        # Never reconnect just to cancel a forward belonging to a dead master.
        if owner is self.process and owner.returncode is None:
            await capture(self.base + ["-O", "cancel", "-L", mapping, self.target])

    async def close(self):
        if self.process:
            await stop_process(self.process)
        if self.diagnostics:
            await self.diagnostics


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
