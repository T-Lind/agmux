# agmux

**A terminal control panel for tmux and AI coding agents.**
One dependency-free Python file. Manage sessions, launch Codex / OpenCode /
Claude Code / … , get told when an agent finishes, and see your usage limits —
without leaving the terminal.

```
agmux 0.2.0                3 sessions · 1 running · 1 done
Codex · 5h 12% · 7d 34%                               Ctrl-G details
Search: 
▶ upgrades-4    Codex     2w  attached  codex  ~/source/repos/ty      4m
✓ agmux        OpenCode  1w            bash   ~/source/repos/agmux  12m  ✓
· scratch      Shell     1w            vim    ~/notes               1h
──────────────────────────────────────────────────────────────────────
codex  |  ~/source/repos/ty
  Fixed the flaky test and reran the suite.
  > All 42 tests passed.
1 of 3 sessions   Enter attach · Ctrl-N new · Ctrl-R rename · Ctrl-D delete · F1 help
```

## Why

tmux is excellent at keeping work alive remotely. It is less good at telling
you *which* session holds which agent, whether that agent is still working, and
whether you have blown through your rate limit. `agmux` is a small, auditable
layer that answers those questions and stays out of the way.

- **One file, no packages.** Standard library only, plus the `tmux` and agent
  binaries you already have. No pip, no plugins, no daemon, no network.
- **Agent-aware.** Knows Codex, OpenCode, Claude Code, Gemini, Aider, Crush, and
  Goose, and can launch any of them — or a plain shell — in a tmux session.
- **Tells you when it's done.** Agent sessions started by agmux are flagged
  `✓` when they finish and fire a desktop notification.
- **Usage at a glance.** Reads the rate-limit windows Codex already writes to
  disk and shows them in the header and in a detail panel.
- **Safe by construction.** No shell command strings, literal argument passing,
  exact-name deletion with revalidation, and filtered terminal output.

## Install

```bash
git clone https://github.com/T-Lind/agmux.git
cd agmux
./install.sh                       # symlinks agmux (and cx) into ~/.local/bin
```

Then add the completion and the tmux popup:

```bash
# ~/.bashrc
source "$HOME/source/repos/agmux/completion.bash"
```

```tmux
# ~/.tmux.conf
source-file ~/source/repos/agmux/tmux.conf
```

Reload with `source ~/.bashrc` and `tmux source-file ~/.tmux.conf`.
If `~/.local/bin` is not on the tmux server's `PATH`, set `AGMUX_BIN` to the
absolute path of the `agmux` script.

## Use

Run `agmux` to open the picker. Type to filter by session name, agent, command,
or directory; select with Up/Down or Tab/Shift-Tab; press Enter to attach (or
switch your current client when already inside tmux). Esc cancels. A preview
shows the selected session's active pane.

| Key | Action |
| --- | --- |
| type | filter sessions |
| `Up`/`Down`, `Tab`/`Shift-Tab` | select |
| `PageUp`/`PageDown`, `Home`/`End` | move through the list |
| `Enter` | attach / switch to the selected session |
| `Ctrl-N` | new session wizard (agent → directory → name) |
| `Ctrl-G` | usage-limit panel |
| `Ctrl-R` | rename the selected session |
| `Ctrl-D` / `Delete` | delete the selected session (typed confirmation) |
| `Ctrl-W` | add a detached shell window in the session's directory |
| `Ctrl-O` | read-only output viewer (keys never reach the pane) |
| `Ctrl-P` | show/hide the preview |
| `Ctrl-L` | refresh now · `Ctrl-U` clear search |
| `F1` | help · `Esc` cancel |

Inside tmux, **`Ctrl-b` then `Ctrl-s`** opens the same picker in a popup and
switches *your* terminal, leaving the running processes untouched.

## Command line

```bash
agmux                                   # the picker
agmux list                              # one line per session (agent, state, dir)
agmux attach upgrades-4                 # switch/attach to a session
agmux new ty-fix --cwd ~/source/repos/ty
agmux new review --agent opencode --cwd ~/source/repos/ty --detach
agmux new notes --agent shell           # a plain shell session
agmux new explain --agent codex -- --model gpt-5   # args after -- go to the agent
agmux rename ty-fix ty-review
agmux shell ty-review --name logs       # add a detached shell window
agmux delete ty-review                  # asks you to type the name
agmux kill ty-review --yes              # alias of delete, skip the typing
agmux usage                             # rate-limit windows, read locally
agmux agents                            # known agents and whether they're installed
agmux preview ty-review                 # recent output, plain text
agmux peek ty-review                    # live read-only viewer
agmux last                              # previous session (inside tmux)
agmux detach
agmux doctor                            # check tools and integration
```

### Agents

`--agent` accepts `codex` (default), `opencode`, `claude`, `gemini`, `aider`,
`crush`, `goose`, or `shell`. `--shell` is a shortcut for `--agent shell`. When
you run `agmux doctor` or `agmux agents`, agmux reports which agent binaries are
actually installed.

For sessions **started outside agmux**, the agent is inferred from the
foreground command in the active pane, so an `opencode` or `codex` process is
labelled even if agmux did not start it.

### Completion notifications

When agmux starts an agent, it wraps the process just enough to notice exit. The
session is marked running, then finished on exit, and agmux:

1. flags the session with `✓` in the picker (cleared when you attach),
2. prints a tmux status message to attached clients, and
3. fires a desktop notification via `notify-send` when a session bus is
   available.

Notifications are on by default. Disable per-session with `--no-notify`, or
globally with `AGMUX_NOTIFY=0`. To route notifications anywhere else, set
`AGMUX_NOTIFY_CMD` to a command with `{title}`, `{body}`, `{session}`, and
`{exit_code}` placeholders — for example:

```bash
export AGMUX_NOTIFY_CMD='ntfy publish mytopic "{body}"'
```

The command is split with shell-like quoting and executed directly; no shell is
involved.

### Usage limits

Codex records token counts and rate-limit windows in its local session logs.
agmux reads the newest one under `$CODEX_HOME/sessions` (default `~/.codex`) and
shows the 5-hour and 7-day windows, percent used, and reset time — in the header
and in the full panel (`Ctrl-G`). This is a local file read; agmux makes **no
network requests** and reads no credentials.

Other agents do not expose comparable local limit data, so the panel says so
rather than guessing.

### Plain shells

You do not need an agent to use agmux. `agmux new scratch --agent shell`, the
picker's `Ctrl-N → Shell`, and `Ctrl-W` all create ordinary tmux sessions and
windows — start a build, tail a log, run `htop`, whatever.

## Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| `AGMUX_AGENT` | default agent for `new` | `codex` |
| `AGMUX_REPOS` | directory listed by the new-session wizard | `~/source/repos` |
| `AGMUX_TMUX_SOCKET` | tmux socket to use | current or default |
| `AGMUX_NOTIFY` | `0` disables completion notifications | `1` |
| `AGMUX_NOTIFY_CMD` | custom notification command | unset |
| `AGMUX_ASCII` | `1` forces ASCII glyphs | unset |
| `AGMUX_BIN` | path to `agmux` used by the tmux popup | `agmux` |
| `CODEX_HOME` | where Codex stores session logs | `~/.codex` |

`CX_REPOS` and `CX_TMUX_SOCKET` are still honored for backwards compatibility.

## Security

agmux is deliberately small so you can read all of it. It uses only the Python
standard library and local `tmux`/agent binaries; it reads local tmux metadata,
directory names, and Codex usage logs; it makes no network requests and reads no
credential files. Session names and pane output are filtered for control
characters before display. Agent arguments are passed as literal argv
(never a shell string). Deletion targets exactly one resolved session and
re-validates it immediately before killing. See [SECURITY.md](SECURITY.md) for
the scope and how it was verified.

## Verification

```bash
python3 -m unittest discover -s tests -v   # isolated tmux socket, no real sessions
python3 tests/ui_smoke.py                  # drives the TUI in private PTYs
bash -n completion.bash
```

Both suites use a private tmux socket and a fake agent binary, so they never
touch your real sessions. The unit suite covers creation, exact selection,
rename, preview, literal argument quoting, deletion confirmation and
revalidation, shell windows, agent detection, usage parsing, notifications, and
`NO_COLOR`/piped output. The smoke test drives the picker, viewer, usage panel,
new-session wizard, popup switching, and delete flow end to end.

## Compatibility

The command was previously named `cx`; `install.sh` keeps a `cx` symlink, and
the old `CX_*` environment variables still work. `completion.bash` completes
both names.

## Uninstall

Remove the `source` lines from `~/.bashrc` and `~/.tmux.conf`, delete
`~/.local/bin/agmux` and `~/.local/bin/cx`, and run `tmux unbind-key C-s` in
your running tmux server. Your sessions remain available through plain tmux.

## License

MIT — see [LICENSE](LICENSE).
