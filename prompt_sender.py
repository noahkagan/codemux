"""Submit a prompt to the root Codex thread in an existing tmux session."""

import asyncio
import json
import os

import status_reader


async def send(session, message, mode="queue"):
    if mode not in ("queue", "steer"):
        raise ValueError("mode must be queue or steer")
    if not isinstance(message, str) or not message.strip() or "\0" in message:
        raise ValueError("prompt must contain text and no NUL characters")
    output = await status_reader.tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
    panes = set()
    for line in output.splitlines():
        name, pid = line.rsplit("\t", 1)
        if name == session:
            panes.add(int(pid))
    if not panes:
        raise ValueError("tmux session not found")
    thread = status_reader.session_thread(panes, status_reader.processes())
    if thread is None:
        raise ValueError("tmux session has no running Codex thread")
    home, thread_id = thread
    environment = dict(os.environ, CODEX_HOME=str(home))
    if mode == "steer":
        return {"session": session, "thread_id": thread_id,
                **await steer(thread_id, message, environment)}
    process = await asyncio.create_subprocess_exec(
        "codex", "queue", "--thread", thread_id, "--message", message,
        env=environment, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("Submission timed out; delivery is unknown. Do not retry blindly.") from None
    if process.returncode:
        raise RuntimeError(error.decode(errors="replace").strip() or
                           output.decode(errors="replace").strip() or "codex queue failed")
    return {"session": session, "thread_id": thread_id, "status": "queued",
            "receipt": output.decode(errors="replace").strip()}


async def steer(thread_id, message, environment):
    from websockets.asyncio.client import unix_connect

    path = os.path.join(environment["CODEX_HOME"], "app-server-control", "app-server-control.sock")

    async def exchange():
        # Connect to the existing daemon. Closing this client never cancels its work.
        async with unix_connect(path, uri="ws://localhost", max_size=None,
                                open_timeout=10, close_timeout=1) as connection:
            request_id = 0

            async def call(method, params):
                nonlocal request_id
                request_id += 1
                await connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
                async for raw in connection:
                    response = json.loads(raw)
                    if response.get("id") != request_id:
                        continue
                    if "error" in response:
                        raise RuntimeError(f"{method}: {response['error']}")
                    return response["result"]
                raise RuntimeError("App-server disconnected; delivery may be unknown")

            await call("initialize", {"clientInfo": {"name": "codemux", "version": "1"}})
            await connection.send(json.dumps({"method": "initialized", "params": {}}))
            snapshot = await call("thread/read", {"threadId": thread_id, "includeTurns": True})
            turns = snapshot["thread"].get("turns", [])
            active = next((turn for turn in reversed(turns) if turn["status"] == "inProgress"), None)
            params = {"threadId": thread_id, "input": [{"type": "text", "text": message}]}
            if active:
                params["expectedTurnId"] = active["id"]
                result = await call("turn/steer", params)
                if result.get("turnId") != active["id"]:
                    raise RuntimeError("Unexpected steering receipt; delivery is unknown")
                return {"status": "steered", "turn_id": result["turnId"]}
            result = await call("turn/start", params)
            turn_id = result["turn"]["id"]
            if not turn_id:
                raise RuntimeError("Missing start receipt; delivery is unknown")
            return {"status": "started", "turn_id": turn_id}

    try:
        return await asyncio.wait_for(exchange(), timeout=30)
    except asyncio.TimeoutError:
        raise RuntimeError("Submission timed out; delivery is unknown. Do not retry blindly.") from None
