# codemux

A small terminal dashboard for remote agent sessions and their web pages.
Select an agent, attach to its tmux session, or forward a web server to your browser.

## Install and run

Requires Linux, Python 3.10+, and OpenSSH `ssh` on the controller.
Teleport hosts also require `tsh`.
The remote hosts need Linux, Python 3.10+, `tmux`, and Codex. Browser launching uses `xdg-open`.
There are no Python packages to install.

```bash
cd ~/codemux
./install.sh
codemux
```

The installer links `~/.local/bin/codemux` to this checkout. Keep the checkout in place;
code changes take effect immediately, and the default `agents.json` stays beside the source.
You can run `codemux` from any directory.

If `~/.local/bin` is not on your PATH, add this to your shell configuration:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Create a local `agents.json` with your hosts and `codex` sessions.
Keep connection details out of version control.
Authenticate with Teleport before starting codemux:

```bash
tsh login --proxy=teleport.example.com
```

## Controls

| Key | Action |
| --- | --- |
| Up / Down or k / j | Select an agent |
| Space | Expand or collapse the selected agent's last final response |
| a | Expand all responses; collapse all when every response is expanded |
| Page Up / Page Down | Scroll through expanded responses |
| Enter | Attach to its remote tmux session, creating the session if absent |
| Ctrl-b, then d | Detach from tmux and return to the dashboard |
| w | Choose a remote HTTP port and launch SSH forwarding |
| o | Open the forwarded URL in your browser |
| x | Stop the selected agent's forward |
| q / Esc | Quit and stop status readers and forwards |

Attachment fills the current terminal. Detaching preserves the remote processes.
An absent tmux session starts a shell; run your agent there yourself.
If you changed tmux's prefix, use your configured detach binding.

Web forwarding binds only to controller loopback and targets remote `127.0.0.1`.
The controller picks a free local port, so several hosts can use the same remote port.
Each URL stays fixed while its forward runs, including while another agent is selected or attached.
Stopping and restarting a forward can assign a different local port.
Quitting codemux closes its forwards and shared SSH connections, leaving remote tmux sessions running.
If a shared connection drops, its forwards stop. Press `w` to recreate a forward after reconnection.

The remote web server must already be running.
Forwarding runs without terminal input; authenticate interactively first if SSH reports an authentication error.
Codemux displays SSH errors but does not probe web servers.

## Shared connections

Codemux opens one OpenSSH master connection per configured host and proxy when status polling starts.
Status readers, tmux attachments, and web forwards share that connection.
Once connected, attachment skips the initial SSH handshake. Remote terminal startup still takes time.
Detaching or stopping a forward leaves the shared connection available.

For Teleport, codemux generates a temporary OpenSSH configuration with `tsh config`.
The generated cluster suffix is added to hostnames automatically; existing agent entries need no changes.
Codemux expects one cluster in that generated configuration.
Plain SSH hosts continue to use your SSH configuration.
Connections authenticate without prompts; log in with `tsh login` or prepare your SSH credentials before starting codemux.

Configuration and control sockets live in a private temporary directory for each codemux instance.
Your SSH configuration is not modified. Quitting closes the connections and removes the temporary directory.
A dropped connection interrupts its active terminal and forwards; status polling reconnects automatically.

## Turn, goal, and response

Every three seconds, a reader maps configured tmux sessions to their root Codex threads and reads local SQLite state.
The reader runs from stdin, installs no files, and opens databases read-only.
It makes no model calls and uses no GPU resources.

The table keeps turn and goal state separate:

- Turn: `active`, `finished`, `interrupted`, `failed`, or `idle` before the first recorded turn.
- Goal: Codex's stored goal state, including `active`, `blocked`, `complete`, `paused`, `usage_limited`, or `budget_limited`.
- `absent` means the tmux session contains no running Codex process.
- `loading` means the first snapshot has not arrived. `unknown` means no usable snapshot is available.
- `stale` means polling failed after an earlier successful snapshot. Cached responses remain available and are labeled as cached.

An active turn can be waiting for approval or user input. This reader does not distinguish those cases.
A finished turn does not mean its goal is complete or that no questions need attention.

Space expands the last final response, preserving paragraphs and wrapping text to the terminal width.
While a new turn runs, the previous final response remains visible with its completion time.
`a` toggles all responses; Page Up and Page Down scroll long responses. Enter continues to attach to tmux.
The selected agent's full host and tmux session remain below the table.
The WEB column shows the local forwarded port; the selected row's full URL appears below the table.

Status polling continues while attached to tmux. SSH failures retry after five seconds; missing snapshots time out after fifteen seconds.
Restart codemux after changing configuration. It needs a terminal at least 72 columns wide and 12 rows high.

The reader uses internal database schemas verified with Codex CLI `0.154.0`.
Codex upgrades can require reader changes; unreadable or unsupported state is shown as unavailable.
Each configured tmux session must contain exactly one root Codex thread. Subagents are excluded.
Sessions with multiple root Codex threads are reported as ambiguous rather than selecting one arbitrarily.

## Agent list

Edit `agents.json` and restart codemux. Each entry has these fields:

| Field | Meaning |
| --- | --- |
| `name` | Label displayed in the agent list |
| `host` | SSH target, such as `user@machine` or an SSH config alias |
| `session` | Remote tmux session name |
| `proxy` | Optional Teleport proxy; omit to use ordinary OpenSSH |
| `web_port` | Suggested remote HTTP port; defaults to 8000 and can be changed with `w` |

Multiple entries can use one host with different tmux sessions.
For plain SSH, connection settings come from `~/.ssh/config`:

```json
[
  {"name": "experiment", "host": "gpu-box", "session": "experiment", "web_port": 3000}
]
```

To use another agent list:

```bash
codemux --config /path/to/agents.json
```

The list is configuration, not discovery. Codemux reads existing state; approvals and agent interaction remain inside the attached terminal.

## Checks

Run the local tests without contacting remote hosts:

```bash
python3 -m unittest discover -s tests -v
```
