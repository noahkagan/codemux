"""Read Codex 0.154 SQLite state without changing sessions or configuration.

The controller sends this file over SSH and runs it in memory on each host.
Only JSON snapshots go to stdout. No files are installed on the remote host.
"""

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3


PROC = Path("/proc")
INTERVAL = 3
TURN_LABELS = {
    "inProgress": "active",
    "completed": "finished",
    "interrupted": "interrupted",
    "failed": "failed",
}


def database(path):
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2))


def processes():
    result = {}
    for directory in PROC.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = dict(line.split(":", 1) for line in (directory / "status").read_text().splitlines())
            result[int(directory.name)] = (int(fields["PPid"]), fields["Name"].strip())
        except (OSError, KeyError, ValueError):
            # Processes can exit while we enumerate /proc.
            continue
    return result


def belongs_to_pane(pid, pane_pids, inventory):
    while pid in inventory:
        if pid in pane_pids:
            return True
        pid = inventory[pid][0]
    return False


def root_threads(pid):
    homes = {}
    for fd in (PROC / str(pid) / "fd").iterdir():
        try:
            path = Path(os.readlink(fd))
        except OSError:
            continue
        if path.parent.name == "thread-writer-locks" and path.suffix == ".lock":
            homes.setdefault(path.parent.parent, set()).add(path.stem)
    roots = set()
    for home, ids in homes.items():
        with database(home / "state_5.sqlite") as state:
            for thread_id in ids:
                row = state.execute("SELECT source FROM threads WHERE id=?", (thread_id,)).fetchone()
                if row and row[0] == "cli":
                    roots.add((home, thread_id))
    return roots


def read_thread(home, thread_id):
    with database(home / "goals_1.sqlite") as goals, database(home / "thread_history_1.sqlite") as history:
        history.execute("BEGIN")
        goal = goals.execute(
            "SELECT status FROM thread_goals WHERE thread_id=?", (thread_id,)).fetchone()
        turn = history.execute(
            "SELECT status, turn_id FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",
            (thread_id,)).fetchone()
        if turn and turn[0] not in TURN_LABELS:
            raise ValueError(f"Unsupported Codex turn state: {turn[0]}")
        row = None
        kind = "progress"
        if turn and turn[0] == "inProgress":
            row = history.execute(
                "SELECT item_id, created_at_ms / 1000.0, item_json FROM thread_items "
                "WHERE thread_id=? AND turn_id=? AND item_type='agentMessage' "
                "ORDER BY rollout_ordinal DESC LIMIT 1", (thread_id, turn[1])).fetchone()
        if row is None:
            kind = "final"
            final = history.execute(
                "SELECT turn_id, final_agent_item_id, completed_at FROM thread_turns "
                "WHERE thread_id=? AND final_agent_item_id IS NOT NULL "
                "ORDER BY rollout_ordinal DESC LIMIT 1", (thread_id,)).fetchone()
            if final:
                row = history.execute(
                    "SELECT item_id, ?, item_json FROM thread_items WHERE thread_id=? AND turn_id=? AND item_id=?",
                    (final[2], thread_id, final[0], final[1])).fetchone()
                if not row:
                    raise ValueError("Codex final response is missing from thread history")
        response = None
        if row:
            item = json.loads(row[2])
            if item.get("type") != "agentMessage" or not isinstance(item.get("text"), str):
                raise ValueError("Unsupported Codex response format")
            if item.get("phase") == "final_answer":
                kind = "final"
            response = {"id": row[0], "text": item["text"], "kind": kind, "timestamp": row[1]}
        return {
            "thread_id": thread_id,
            "turn": TURN_LABELS[turn[0]] if turn else "idle",
            "goal": goal[0] if goal else None,
            "response": response,
        }


def codex_processes(pane_pids, inventory):
    return [pid for pid, (_, name) in inventory.items()
            if name == "codex" and belongs_to_pane(pid, pane_pids, inventory)]


def read_session(pane_pids, inventory):
    codex_pids = codex_processes(pane_pids, inventory)
    if not codex_pids:
        return {"thread_id": None, "turn": "absent", "goal": None, "response": None}
    roots = set().union(*(root_threads(pid) for pid in codex_pids))
    if len(roots) != 1:
        raise ValueError(f"Expected one root Codex thread in this tmux session; found {len(roots)}")
    return read_thread(*next(iter(roots)))


def approval_visible(screen):
    # Match the current modal's controls, never approval text in scrollback.
    lines = "\n".join(line.strip() for line in screen.splitlines() if line.strip())
    return (lines.endswith("Press enter to confirm or esc to cancel")
            and re.search(r"^(?:› )?\d+\. Yes, proceed \(y\)$", lines, re.MULTILINE) is not None
            and re.search(r"^(?:› )?\d+\. No, and tell Codex what to do differently \(esc\)$", lines, re.MULTILINE) is not None)


async def tmux(*arguments):
    process = await asyncio.create_subprocess_exec(
        "tmux", *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout=3)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("tmux did not respond")
    if process.returncode:
        raise RuntimeError(error.decode(errors="replace").strip() or "tmux is unavailable")
    return output.decode()


async def snapshot(sessions):
    output = await tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}\t#{pane_id}")
    panes = {}
    for line in output.splitlines():
        session, pid, pane_id = line.rsplit("\t", 2)
        panes.setdefault(session, {})[int(pid)] = pane_id
    inventory = processes()
    result = {}
    for session in sessions:
        try:
            if session not in panes:
                raise ValueError("tmux session not found")
            value = read_session(panes[session], inventory)
            if value["turn"] == "active":
                for pid, pane_id in panes[session].items():
                    if codex_processes({pid}, inventory):
                        screen = await tmux("capture-pane", "-p", "-J", "-t", pane_id)
                        if approval_visible(screen):
                            value["turn"] = "approval"
                            break
            result[session] = value
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
            result[session] = {"error": str(error)}
    return result


async def run(sessions, once=False):
    while True:
        try:
            values = await snapshot(sessions)
        except (OSError, RuntimeError, ValueError) as error:
            values = {session: {"error": str(error)} for session in sessions}
        print(json.dumps(values), flush=True)
        if once:
            return
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("sessions", nargs="+")
    args = parser.parse_args()
    asyncio.run(run(args.sessions, args.once))
