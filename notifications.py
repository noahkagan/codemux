"""Desktop notifications for observed agent stops."""

import asyncio
import html
import textwrap

from ssh_transport import capture


def stopped_state(snapshot):
    turn = snapshot["turn"]
    if turn in ("approval", "failed", "interrupted", "absent"):
        return {"approval": "Approval required", "absent": "Stopped"}.get(turn, turn.capitalize())
    if turn != "finished" or snapshot.get("goal") == "active":
        return None
    return {
        "blocked": "Goal blocked", "paused": "Goal paused",
        "usage_limited": "Usage limit reached", "budget_limited": "Budget limit reached",
        "complete": "Goal complete",
    }.get(snapshot.get("goal"), "Finished")


class Notifications:
    def __init__(self):
        self.previous = {}
        self.queue = asyncio.Queue()
        self.error = None

    def observe(self, agent, snapshot):
        if snapshot.get("error"):
            return
        previous = self.previous.get(agent)
        self.previous[agent] = snapshot
        # The first successful snapshot establishes a baseline without a startup burst.
        if previous is None:
            return
        state = stopped_state(snapshot)
        identity = (snapshot.get("thread_id"), snapshot.get("turn_id"), state)
        old_identity = (previous.get("thread_id"), previous.get("turn_id"), stopped_state(previous))
        if state is None or identity == old_identity:
            return
        response = snapshot.get("response") or previous.get("response")
        text = response["text"] if response else "No response available yet."
        text = " ".join(text.split())
        text = "".join(char for char in text if char.isprintable())
        snippet = textwrap.shorten(text, width=240, placeholder="…")
        label = "Last progress" if response and response.get("kind") == "progress" else "Last response"
        self.queue.put_nowait((f"{agent.name} — {state}", html.escape(f"{agent.host}\n{label}: {snippet}")))

    async def run(self):
        while True:
            title, body = await self.queue.get()
            try:
                await capture(["notify-send", "--app-name=codemux", "--urgency=normal", "--", title, body])
            except asyncio.TimeoutError:
                self.error = "Desktop notification timed out."
            except (OSError, RuntimeError) as error:
                self.error = f"Desktop notification failed: {error}"
            finally:
                self.queue.task_done()
