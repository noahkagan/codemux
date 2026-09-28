# codemux

A small terminal dashboard for remote agent sessions and their web pages.
Select an agent, attach to its tmux session, or forward a web server to your browser.

## Install and run

Requires Linux, Python 3.10+, and OpenSSH `ssh` on the controller.
Teleport hosts also require `tsh`.
The remote hosts need Linux, Python 3.10+, `tmux`, and Codex. Browser launching uses `xdg-open`.
Desktop notifications use `notify-send` and the controller's desktop notification service.
There are no Python packages to install.

```bash
cd ~/codemux
./install.sh
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/codemux"
mkdir -p "$config_dir"
cp -n agents.example.json "$config_dir/agents.json"
```

Edit that configuration with your SSH hosts, tmux sessions, and optional Teleport proxy, then run `codemux`.
The example uses placeholder hosts and does not connect to a real deployment.

The installer links `~/.local/bin/codemux` to this checkout. Keep the checkout in place;
code changes take effect after restarting codemux. Agent configuration stays outside the checkout.
You can run `codemux` from any directory.

If `~/.local/bin` is not on your PATH, add this to your shell configuration:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

If you use Teleport, authenticate with your configured proxy before starting codemux:

```bash
tsh login --proxy=teleport.example.com
```

If the Teleport certificate expires, run that login command again in another terminal.
The dashboard shows the command for your configured proxy and retries SSH connections every five seconds.

## Controls

| Key | Action |
| --- | --- |
| Up / Down or k / j | Select an agent |
| Space | Expand or collapse the selected agent's progress or final response |
| a | Expand all responses; collapse all when every response is expanded |
| n | Edit the selected agent's note; Enter saves, Esc cancels, Ctrl-U clears |
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

## Desktop notifications

Codemux sends a desktop notification when an agent finishes, fails, is interrupted, awaits approval, or its Codex process stops.
Notifications include the agent name, host, and a snippet of the latest response or progress message, limited to 240 characters.
If the process exits, the notification uses its last observed response.

Completed turns inside an active `/goal` loop stay quiet.
A finished turn with a complete, blocked, paused, usage-limited, or budget-limited goal triggers a notification.
Approval requests, failures, and interruptions notify even when the goal remains active.

The first successful snapshot establishes a baseline without notifying. Repeated snapshots and reconnections do not repeat the same alert.
Notifications continue while attached to tmux and stop when codemux exits.
They depend on the three-second polling interval and the status reader's approval detection limits.
Unknown or stale status does not count as an observed stop.

Notification delivery runs in the background. Missing `notify-send` or delivery errors appear in the dashboard's message line.
Your desktop's notification settings control whether banners appear.

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

- Turn: `active`, `approval`, `finished`, `interrupted`, `failed`, or `idle` before the first recorded turn.
- Goal: Codex's stored goal state, including `active`, `blocked`, `complete`, `paused`, `usage_limited`, or `budget_limited`.
- `absent` means the tmux session contains no running Codex process.
- `loading` means the first snapshot has not arrived. `unknown` means no usable snapshot is available.
- `stale` means polling failed after an earlier successful snapshot. Cached responses remain available and are labeled as cached.

Rows use green for running turns, gray for finished or idle agents, red for failures, and cyan while loading.
Yellow marks approval prompts, interrupted turns, unavailable status, and blocked, paused, or limited goals when the turn is not running.
The selected row retains its color with a reversed background. Terminals without color retain the text labels.

Codex's database keeps turns active while awaiting approval.
For active turns, codemux also reads the visible tmux panes containing Codex and recognizes the standard approval dialog's controls.
Recognized dialogs show `approval` in yellow; the next poll clears this label once the dialog disappears.
This detection depends on Codex's terminal format and can miss clipped or different dialogs. Other user-input prompts can still appear as `active`.
Codemux does not read scrollback or answer prompts. Enter attaches to tmux so you can review the request there.
A finished turn does not mean its goal is complete or that no questions need attention.

The PROGRESS column follows HOST and uses the remaining terminal width for a single-line message preview.
It shows current progress during active turns and the final response afterward. Long messages end with an ellipsis.
HOST fits the configured hostnames when space allows; narrow terminals shorten names to keep status columns visible.
Cached previews are marked `[cached]` when polling fails.

Space expands the latest progress message during an active turn, preserving paragraphs and wrapping text to the terminal width.
The preview updates every three seconds as Codex records messages, then shows the final response when the turn finishes.
Until the current turn records a message, the previous final response remains visible.
Labels distinguish progress from final responses and show the message creation or turn completion time, respectively.
The preview excludes tool output and reasoning; it is not a live terminal feed.
`a` toggles all responses; Page Up and Page Down scroll long responses. Enter continues to attach to tmux.
The selected agent's full host and tmux session remain below the table.
The WEB column shows the local forwarded port; the selected row's full URL appears below the table.

Manual notes appear beneath agent rows, including when responses are collapsed.
Press `n` to edit a note. Type to append, Backspace to delete, or Ctrl-U to clear the text.
Enter saves; saving an empty note removes it. Esc cancels the edit.
Notes persist in the agent configuration, including when using `--config`, and are never sent to remote agents.
Long notes are shortened in the table; press `n` to edit them.

Status polling continues while attached to tmux. SSH failures retry after five seconds; missing snapshots time out after fifteen seconds.
Restart codemux after changing configuration. It needs a terminal at least 72 columns wide and 12 rows high.

The reader uses internal database schemas verified with Codex CLI `0.154.0`.
Codex upgrades can require reader changes; unreadable or unsupported state is shown as unavailable.
Each configured tmux session must contain exactly one root Codex thread. Subagents are excluded.
Sessions with multiple root Codex threads are reported as ambiguous rather than selecting one arbitrarily.

## Agent list

The default agent list is `$XDG_CONFIG_HOME/codemux/agents.json`, or `~/.config/codemux/agents.json` when that variable is unset or empty.
Edit that file and restart codemux. Each entry has these fields:

| Field | Meaning |
| --- | --- |
| `name` | Label displayed in the agent list |
| `host` | SSH target, such as `user@machine` or an SSH config alias |
| `session` | Remote tmux session name |
| `proxy` | Optional Teleport proxy; omit to use ordinary OpenSSH |
| `web_port` | Suggested remote HTTP port; defaults to 8000 and can be changed with `w` |
| `note` | Optional manual annotation; edit with `n` in the dashboard |

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
Only `agents.example.json` belongs in version control. Keep real hostnames and connection details in your user configuration or an external `--config` file.
The repository ignores a root-level `agents.json` to prevent accidentally adding a local copy.

## Checks

Run the local tests without contacting remote hosts:

```bash
python3 -m unittest discover -s tests -v
```

## License

Licensed under the [Zero-Clause BSD license (0BSD)](LICENSE). No attribution is required.
