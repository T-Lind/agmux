# agmux

**A terminal control panel for tmux and AI coding agents.**
One dependency-free Python file. Manage sessions running Codex / OpenCode /
Claude Code / …, see their state and resource use, get told when an agent
finishes or needs you, and check your usage limits — without leaving the
terminal.

![The agmux picker: sessions with agent state, a live preview, and per-session CPU and memory](docs/screenshot.svg)

```
agmux 0.7.0                     5 sessions · 2 running · 1 need input · 1 done
Codex · 5h 12% · 7d 34%       OpenCode 392K tok · $0.27        Ctrl-G details
Search:
! ty-review    Codex    2w  needs input  cpu 12% · mem 486M  codex  ~/source/repos/ty     2m
▶ upgrades-4   Codex    2w  attached     cpu 37% · mem 611M  codex  ~/source/repos/ty     4m
▶ opencode-ty  OpenCode 1w                cpu  4% · mem 228M  bun    ~/source/repos/ty     4s
✓ agmux        OpenCode 1w                cpu  1% · mem 233M  bun    ~/source/repos/agmux  12m ✓
· scratch      Shell    1w                cpu  0% · mem  12M  vim    ~/notes               1h
──────────────────────────────────────────────────────────────────────────────────────────
codex  |  ~/source/repos/ty
! needs input: Allow command?  1. Yes  2. Yes, and don't ask again  3. No
1 of 5 sessions   Enter attach · Ctrl-N new · Ctrl-R rename · Ctrl-D delete · F1 help
```

Here `cpu` is the machine's CPU usage and `mem` is resident RAM — both for the
session's own processes, **not** token, context-window, or rate-limit usage
(those are the percentages in the line above, from `Ctrl-G`).

## Why

tmux is excellent at keeping work alive remotely. It is less good at telling
you *which* session holds which agent, whether that agent is still working,
how much CPU and memory each session is burning, and whether you have blown
through your rate limit. `agmux` is a small, auditable layer that answers those
questions and stays out of the way.

- **One file, no packages.** Standard library only, plus the `tmux` and agent
  binaries you already have. No pip, no plugins, no installed service, no network.
- **Agent-aware.** Knows Codex, OpenCode, Claude Code, Gemini, Aider, Crush, and
  Goose, and can launch any of them — or a plain shell — in a tmux session.
- **Resource use per session.** A labelled `cpu X% · mem Y` figure next to each
  session, summed across all of that session's panes and their child processes —
  machine CPU and RAM, kept visually distinct from token and rate-limit usage.
- **Tells you when it's done.** Agent sessions started by agmux are flagged
  `✓` when they finish and fire a desktop notification; attaching to the
  session clears the flag.
- **Tells you when it needs you.** If an agent is waiting on a permission or
  confirmation prompt, agmux flags it `!`, sorts it to the top, and can notify
  you — so you are not left staring at a stalled session. It keeps watching the
  agent's own pane even if you switch to another window in that session.
- **Live previews that stay current.** The preview and the read-only viewer
  render the pane's own colors and styles, always end on the newest output even
  if someone scrolled the pane up, and trim the agent's input composer and
  status footer so you see real output.
- **Usage at a glance.** Reads Codex's rate-limit windows from its logs and
  OpenCode's per-session cost/tokens from its database, side by side in the
  header and in a detail panel. `usage --json` makes the same data available to
  scripts.
- **Inspect before you act.** `agmux info` shows one session's agent, state,
  panes, clients, directory, CPU, and memory; `list --json` and `usage --json`
  cover the rest without scraping the picker.
- **Workspaces survive reboot.** Automatically saves session names, windows,
  panes, layouts, and directories. Saved sessions appear in the picker; Enter
  restores one. Closed sessions stay available until you forget them.
- **Copy output.** Open the read-only viewer with `Ctrl-O`, drag or use `v` and
  arrow keys to select text, then press `y` to copy it to your clipboard.
- **Portable settings.** An optional config file carries your project roots and
  preferences between machines.
- **Safe by construction.** No shell command strings, literal argument passing,
  exact-name deletion with revalidation, and filtered terminal output.

## Install (Linux)

Requires Python 3.8+ and `tmux`; agent sessions additionally need whichever
agent binaries you already use. Nothing is downloaded at runtime.

```bash
git clone https://github.com/T-Lind/agmux.git
cd agmux
./install.sh
```

`install.sh` is one command that:

1. links `agmux` (and the historical `cx`) into `~/.local/bin`,
2. adds bash completion to `~/.bashrc`,
3. adds the `Ctrl-b Ctrl-s` popup and the prefix-less `Alt-s` / `Alt-x`
   bindings to `~/.tmux.conf`,
4. installs the `agmux(1)` man page, and
5. tells you if `~/.local/bin` is missing from `PATH`.

It is idempotent and reversible:

```bash
./install.sh --bin ~/bin      # choose where to link the command
./install.sh --no-integration # link only; do not touch dotfiles
./install.sh --uninstall      # remove the links and the managed blocks
```

Then reload: `source ~/.bashrc` and `tmux source-file ~/.tmux.conf`.
`make install` / `make uninstall` are thin wrappers over the same script.
Check everything with `agmux doctor`.

## Use

Run `agmux` to open the picker. Type to filter by session name, agent, command,
directory, or state; select with Up/Down or Tab/Shift-Tab; press Enter to attach
(or switch your current client when already inside tmux). Esc cancels. A preview
shows the selected session's active pane.

| Key | Action |
| --- | --- |
| type | filter sessions |
| `Up`/`Down`, `Tab`/`Shift-Tab` | select |
| `PageUp`/`PageDown`, `Home`/`End` | move through the list |
| `Enter` | attach / switch, or restore a saved workspace |
| `Ctrl-N` | new session wizard (agent → directory → name) |
| `Ctrl-G` | usage-limit panel |
| `Ctrl-R` | rename the selected session |
| `Ctrl-D` / `Delete` | delete a live session or forget a saved workspace (typed confirmation) |
| `Ctrl-W` | add a detached shell window in the session's directory |
| `Ctrl-O` | read-only output viewer with text selection and copy |
| `Ctrl-P` | show/hide the preview |
| `Ctrl-L` | refresh now · `Ctrl-U` clear search |
| `F1` | help · `Esc` cancel |

Inside tmux, **`Ctrl-b` then `Ctrl-s`** opens the same picker in a popup and
switches *your* terminal, leaving the running processes untouched.

The shipped tmux integration also installs two prefix-less bindings so you do
not need the `Ctrl-b` prefix while attached:

| Binding | Action |
| --- | --- |
| `Alt-s` | detach this client and open the agmux picker in the same terminal |
| `Alt-x` | kill the current session after a `y/n` confirmation |

`Alt-s` leaves the tmux client, so agmux runs directly and selecting a session
attaches you to it; `Esc` in the picker returns you to the shell. The picker is
not overlaid on the old session in this mode. Use `Ctrl-b Ctrl-s` if you prefer
the popup that stays inside tmux.

`Alt-x` stops every program in that session and detaches its clients. Use the
picker's `Ctrl-D` flow instead if you want agmux's typed-name confirmation and
change revalidation. Terminal support for Alt combinations varies; `Ctrl-b
Ctrl-s` remains the portable fallback.

## Command line

```bash
agmux                                   # the picker
agmux list                              # one line per session (agent, state, dir, CPU/mem)
agmux list --json                       # machine-readable sessions
agmux list --no-resources               # skip the CPU/memory sample
agmux info ty-review                    # detailed view of one session
agmux info ty-review --json             # the same details as JSON
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
agmux usage --json                      # Codex limits + OpenCode cost as JSON
agmux agents                            # known agents and whether they're installed
agmux preview ty-review                 # recent output, with the pane's colors
agmux peek ty-review                    # live read-only viewer
agmux watch                             # notify when an agent needs input
agmux last                              # previous session (inside tmux)
agmux detach
agmux save                              # snapshot all live workspaces now
agmux saved                             # sessions available for recovery (--json supported)
agmux restore ty-review                  # recreate one saved workspace, detached
agmux restore --all                      # recreate all missing saved workspaces
agmux restore ty-review --fresh          # explicitly replay the original agent launch
agmux forget ty-review                   # forget a saved definition (typed confirmation)
agmux doctor                            # check tools, recovery, usage sources, and integration
```

### After reboot: workspace recovery

Recovery is enabled by default. Opening agmux or creating a session saves the
live tmux workspace and starts one small background saver for that tmux server.
It updates the snapshot every 15 seconds, even when the picker is closed, and
exits after the server stops. Changes made through agmux (creation, rename,
adding a shell window, and deletion) also trigger a snapshot immediately.
Run `agmux save` once after upgrading to capture sessions you already have.

After reboot, open `agmux` as usual. Your missing sessions appear as **saved**;
select one and press Enter to restore and attach. You can also run
`agmux restore NAME` or `agmux restore --all` to restore them detached. This
recreates window names and indices, panes, split layouts, zoom, focus, and each
pane's directory. It skips live names instead of replacing them when restoring
all. A missing directory or failed restore keeps the saved definition for retry,
and restoring all continues with the other sessions.

Running processes and terminal scrollback are not checkpointed. For Codex,
restoring opens `codex resume` so you can choose the saved conversation. When
agmux knows an explicit conversation ID from `new ... -- resume ID`, or captures
Codex's resume instruction at normal exit, it resumes that ID directly. It never
uses `--last` to guess between conversations. Other agents restore to shell
panes; start the agent's own resume flow there. `--fresh` explicitly starts the
saved agent with its original arguments, including any original task prompt.

Closed sessions remain recoverable. `Ctrl-D` on a saved row, or
`agmux forget NAME`, removes its definition after typed confirmation (`--yes`
for scripts). Forgetting does not kill a live session; a live session will be
saved again on the next checkpoint. Renaming moves the saved definition.

Snapshots are stored separately per tmux socket under
`${XDG_STATE_HOME:-~/.local/state}/agmux`. They use private files (mode `0600`)
and atomic replacement with a writer lock. They contain workspace paths, agent
launch arguments, and known resume IDs, not pane output or transcripts. If launch
arguments contain sensitive text, that text is included in this private file.
`agmux doctor` reports its path and recovery status. Invalid snapshots are
reported and preserved instead of silently overwritten.

Set `persist = 0` / `AGMUX_PERSIST=0` to disable automatic saving and saved rows
in the picker; the next agmux invocation stops an existing saver on its next
check. Explicit `save`, `saved`, and `restore` commands still work. Set
`save_interval = 0` to disable just the background saver, keeping snapshots on
agmux changes. Override the storage directory with `state_dir` / `AGMUX_STATE_DIR`.
Recovery happens when you use agmux; it does not open a terminal at OS login.

### Select and copy output

Press `Ctrl-O` in the picker, or run `agmux peek NAME`, to open the read-only
viewer. Drag over output with the mouse, or press `v` to start selection and use
arrow keys / `hjkl` to extend it. `Home` / `End` select to the beginning/end of
the line; `PageUp` / `PageDown` move by a screen, and `g` / `G` move to the oldest /
newest captured line. Press `y` (or `Ctrl-Y`) to copy. Esc cancels the selection;
Esc again returns to the picker. Output is frozen during selection so new agent
output cannot change what you copy. The viewer captures up to 500 recent lines.

Copy uses `wl-copy` on Wayland, `xclip` / `xsel` on X11, or `pbcopy` on macOS
when available. Inside tmux it can copy to tmux's paste buffer and request the
terminal clipboard (`set-clipboard` must permit it); otherwise it sends an
OSC 52 clipboard request to supporting terminals, including over SSH. Terminal
permissions can block that request, so the status reports delivery rather than
claiming clipboard success. The OSC 52 fallback through tmux requires
`allow-passthrough on`. Hold Shift while dragging to use your terminal's own
selection instead of agmux's mouse selection. No viewer keys reach the agent.

### Agents

`--agent` accepts `codex` (default), `opencode`, `claude`, `gemini`, `aider`,
`crush`, `goose`, or `shell`. `--shell` is a shortcut for `--agent shell`. When
you run `agmux doctor` or `agmux agents`, agmux reports which agent binaries are
actually installed.

For sessions **started outside agmux**, the agent is inferred from the
foreground command in the active pane, so an `opencode` or `codex` process is
labelled even if agmux did not start it.

### Checking your setup

`agmux doctor` prints the resolved tmux socket and version, whether the tmux
build supports the `Ctrl-b Ctrl-s` popup, where the helper and config live,
which agent binaries are installed, notification and CPU/memory status, the
Codex log and OpenCode database sources it can actually read, ASCII/color
settings, and a session count with how many need input or have finished. It is
the quickest way to tell "agmux has no data" apart from "agmux is not reading the
right file".

### CPU and memory per session

Each row shows `cpu X%` and `mem Y` for the whole session: the active process in
every pane plus its descendants. The `cpu` and `mem` words are deliberate —
these are the machine's CPU and RAM used by that session, not token,
context-window, or rate-limit figures. It is sampled once per refresh from `ps`
(pid, parent, CPU%, RSS only), so one query covers every session, and is off by
default on platforms without a compatible `ps`. Set `AGMUX_RESOURCES=0` or pass
`--no-resources` to hide it. In JSON the fields are `cpu` (percent) and `rss`
(kibibytes):

```bash
agmux list --json | jq '.[] | {name, status, cpu, rss}'
```

`list --json` also reports `clients`, `unread`, `agent`, `state`, `prompt`, and
`cwd`. `agmux info SESSION` adds per-pane detail and the attached client ttys;
`agmux info SESSION --json` is useful for debugging a session that looks stuck.

### Previews always show the newest output

The preview and the read-only viewer capture to the *end* of the pane's
history, so they end on the latest line even when the pane (or the client
viewing it) has been scrolled up — you always see what the agent is doing now.
The agent's input composer and status footer are trimmed so real output fills
the space, and short output hugs the bottom like a real terminal. Trimming
understands Codex, OpenCode, Claude Code, Gemini, Aider, Crush, and Goose
footers; set `AGMUX_TRIM_CHROME=0` to keep the agent chrome.

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

### When an agent needs you

agmux reads the active pane's visible screen and classifies each agent session
as **working**, **idle** (alive at its prompt), or **blocked** (a permission,
confirmation, or question prompt). Blocked sessions are sorted to the top of the
picker and flagged `!`, and the picker header counts how many need input. The
detected prompt line is shown above the preview and appended to `agmux list`.

For agent sessions agmux starts, a small watcher thread polls the agent's pane
while the agent runs and raises the same desktop/tmux notification for a blocked
prompt. It remembers which pane it started, so switching to a shell window in
the same session does not hide a waiting prompt, and it re-notifies if the same
prompt appears again after the agent resumes. For sessions started elsewhere,
run the standalone watcher, which notifies on any transition into the blocked
state:

```bash
agmux watch                # poll every few seconds until interrupted
agmux watch --once         # a single pass (handy in scripts)
```

Detection is heuristic — agents draw their prompts differently and change them
over time. The defaults cover common `[y/N]` / "Allow …" / "Would you like to …"
/ numbered-approval forms. Extend them with `AGMUX_BLOCKED_PATTERNS` and
`AGMUX_WORKING_PATTERNS`: `'|'`-joined regular expressions appended to the
built-ins, matched case-insensitively against the last screen lines.

Entering a session clears any tmux copy-mode on its panes (`send-keys -X
cancel`), so you always land on the live view rather than a stale scrollback.

### Usage limits

**Codex.** Codex records token counts and rate-limit windows in its local
session logs. agmux reads the newest one under `$CODEX_HOME/sessions` (default
`~/.codex`) and shows the 5-hour and 7-day windows, percent used, and reset
time — in the header and in the full panel (`Ctrl-G`).

**OpenCode.** OpenCode does not expose account rate limits locally, but it
stores per-session cost and token counts in its SQLite database. agmux reads
that (read-only) and shows recent sessions' cost, tokens, and age next to the
Codex windows. It understands both the older `session` table and the newer
`session_v2` schema, includes reasoning/cache tokens when recorded, and handles
timestamps stored in seconds or milliseconds. Set `OPENCODE_DATA_DIR` if your
database lives elsewhere.

Both are local file reads; agmux makes **no network requests** and reads no
credentials. Other agents do not expose comparable local data, so the panel
says so rather than guessing.

## Projects and configuration

The new-session wizard lists the immediate subdirectories of your **project
roots** as candidate working directories. By default that is `~/source/repos`;
set `AGMUX_REPOS` to change it. It accepts several roots separated by `:`, and
`~` is expanded. The wizard also offers the current directory, your home
directory, and an **Other directory** entry where you can type any path:

```bash
export AGMUX_REPOS="$HOME/source/repos:$HOME/work:$HOME/notes"
```

These are just convenient launch folders — agmux lists and manages sessions from
your **local tmux server**, and every session stores its own working directory.

### A config file instead of a wall of exports

For settings you want on every shell, put them in an optional
`~/.config/agmux/config` (override with `AGMUX_CONFIG`, which must be set
*before* startup). Environment variables always win, so you can keep shared
defaults in the file and override them per shell:

```ini
# ~/.config/agmux/config
repos = ~/source/repos:~/work
agent = codex
resources = 1

# extra "needs input" heuristics, '|'-joined regexes
blocked_patterns = \[y/N\]|continue\?
```

Recognized keys: `persist`, `state_dir`, `save_interval`, `repos`, `agent`, `tmux_socket`, `notify`, `notify_cmd`,
`watch_interval`, `blocked_patterns`, `working_patterns`, `trim_chrome`,
`resources`, `ascii`, `bin`, `codex_home`, `opencode_data_dir`.

### Using it across machines

agmux is one file and talks to whichever tmux server it is pointed at, so the
usual setup is to install it on each machine (`git clone && ./install.sh`) and
keep `~/.config/agmux/config` in your dotfiles. Sessions do **not** federate
across machines — a session lives on the tmux server where it was created.
To work with a remote machine, run agmux there (for example over SSH, or via
`tmux -L`/`AGMUX_TMUX_SOCKET` against a remote socket). agmux itself still makes
no network connections; it only ever reads the local tmux server and local
files.

### Plain shells

You do not need an agent to use agmux. `agmux new scratch --agent shell`, the
picker's `Ctrl-N → Shell`, and `Ctrl-W` all create ordinary tmux sessions and
windows — start a build, tail a log, run `htop`, whatever.

## Configuration reference

Every setting has an environment variable; all are optional.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AGMUX_PERSIST` | `0` disables automatic recovery saving and saved picker rows | `1` |
| `AGMUX_STATE_DIR` | directory for private workspace snapshots | `$XDG_STATE_HOME/agmux` or `~/.local/state/agmux` |
| `AGMUX_SAVE_INTERVAL` | background checkpoint interval in seconds; `0` disables the background saver | `15` |
| `AGMUX_AGENT` | default agent for `new` | `codex` |
| `AGMUX_REPOS` | project roots listed by the new-session wizard (`:`-separated) | `~/source/repos` |
| `AGMUX_CONFIG` | config file to load | `~/.config/agmux/config` |
| `AGMUX_TMUX_SOCKET` | tmux socket to use | current or default |
| `AGMUX_RESOURCES` | `0` hides per-session CPU/memory | `1` |
| `AGMUX_NOTIFY` | `0` disables completion notifications | `1` |
| `AGMUX_NOTIFY_CMD` | custom notification command | unset |
| `AGMUX_WATCH_INTERVAL` | seconds between blocked-prompt checks | `3` |
| `AGMUX_BLOCKED_PATTERNS` | extra `|`-joined regexes for "needs input" | unset |
| `AGMUX_WORKING_PATTERNS` | extra `|`-joined regexes for "working" | unset |
| `AGMUX_TRIM_CHROME` | `0` keeps agent input/footer lines in previews | `1` |
| `AGMUX_ASCII` | `1` forces ASCII glyphs | unset |
| `AGMUX_BIN` | path to `agmux` used by the tmux popup | `agmux` |
| `CODEX_HOME` | where Codex stores session logs | `~/.codex` |
| `OPENCODE_DATA_DIR` | where OpenCode stores its database | `~/.local/share/opencode` |

`CX_REPOS` and `CX_TMUX_SOCKET` are still honored for backwards compatibility.

## Security

agmux is deliberately small so you can read all of it. It uses only the Python
standard library and local `tmux`/agent binaries; it reads local tmux metadata,
directory names, private recovery snapshots, one `ps` process listing (pid, parent, CPU%, RSS), the optional
config file, and Codex/OpenCode usage logs; it makes no network requests and
reads no credential files. Session names and pane output are filtered for
control characters before display. Agent arguments are passed as literal argv
(never a shell string). Deletion targets exactly one resolved session and
re-validates it immediately before killing. See [SECURITY.md](SECURITY.md) for
the scope and how it was verified.

## Verification

```bash
make test                                  # lint + unit tests + UI smoke test
python3 -m unittest discover -s tests -v   # isolated tmux socket, no real sessions
python3 tests/ui_smoke.py                  # drives the TUI in private PTYs
bash -n install.sh completion.bash
```

Both suites use a private tmux socket and a fake agent binary, so they never
touch your real sessions. The unit suite covers creation, exact selection,
rename, preview, literal argument quoting, deletion confirmation and
revalidation, shell windows, agent detection, per-session CPU/memory sampling
(including the BSD `ps` fallback), previews staying on the newest output while
scrolled, tracked agent panes across window switches, multi-root project
listing, ANSI parsing/rendering, screen classification for Codex/OpenCode/
Claude/Gemini/Aider prompts, copy-mode cancellation, Codex and OpenCode usage
parsing (including old OpenCode schemas), notifications, config loading,
`list`/`info`/`usage` JSON, workspace recovery after server shutdown, layout/zoom/directory restoration,
atomic private snapshots, failed/partial recovery, safe resume arguments,
background saving, text selection and clipboard backends, and `NO_COLOR`/piped output. The smoke test drives
the picker, viewer, usage panel, new-session wizard, popup switching, and delete
flow end to end, including saved-session recovery and clipboard selection.

## Compatibility

The command was previously named `cx`; `install.sh` keeps a `cx` symlink, and
the old `CX_*` environment variables still work. `completion.bash` completes
both names. Linux is supported. Other Unix systems work when their `ps` supports
`-eo pid=,ppid=,pcpu=,rss=` or `-A -o pid=,ppid=,pcpu=,rss=` (macOS uses the
latter); otherwise agmux simply shows no resource figures.

## Uninstall

```bash
./install.sh --uninstall       # or: make uninstall
```

This removes the `agmux`/`cx` links, the managed blocks in `~/.bashrc` and
`~/.tmux.conf`, and leaves your sessions and dotfiles otherwise intact.

## License

MIT — see [LICENSE](LICENSE).
