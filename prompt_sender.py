"""Submit a prompt to the root Codex thread in an existing tmux session."""

import asyncio
import os

import status_reader


async def send(session, message):
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
