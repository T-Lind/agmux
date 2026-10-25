# Security and scope

Scope: `agmux.py`, `install.sh`, `completion.bash`, `tmux.conf`, `agmux.1`,
`Makefile`, and their installed source lines in `~/.bashrc` and `~/.tmux.conf`.
agmux is one standard-library Python file plus the `tmux` and agent binaries
already on the machine. There is no vendor, updater, background service, or
network client.

## What agmux accesses

- Python source and standard-library modules.
- Names of immediate directories under `AGMUX_REPOS` (default `~/source/repos`).
- tmux's local Unix socket, session/client metadata, per-session user options,
  pane working directories, foreground commands, and displayed pane output.
- The newest Codex session logs under `$CODEX_HOME/sessions` (default
  `~/.codex/sessions`), and only their trailing bytes, to read the most recent
  `rate_limits` record for the usage panel. No other part of the log is
  displayed or stored.
- OpenCode's SQLite database under `$OPENCODE_DATA_DIR` (default
  `~/.local/share/opencode`), opened read-only, and only the session title,
  cost, token counts (including reasoning/cache when present), model, and update
  time of the most recent sessions. No credential, message, or account tables
  are read.
- The visible text of a session's pane, read with `capture-pane`, to label the
  agent and to classify whether it is working, idle, or waiting on the user, and
  to trim the agent's input/status chrome from previews. For agents agmux
  started, it reads the recorded agent pane even if another window in the same
  session is currently focused.
- One system process listing (`ps -eo pid=,ppid=,pcpu=,rss=` or the BSD
  `ps -A -o pid=,ppid=,pcpu=,rss=` equivalent) to show per-session CPU and
  memory: only pid, parent pid, CPU percentage, and resident set size are read;
  process arguments and environments are never requested or shown.
- An optional `key = value` config file at `$AGMUX_CONFIG` or
  `~/.config/agmux/config`, used only to set defaults for the variables below.
- Executable locations on `PATH` for tmux and the agent binaries, and the
  normal shell path.
- Settings such as `TMUX`, `TMUX_PANE`, `AGMUX_*`, `CX_*`, and `SHELL`.

agmux contains no HTTP client, socket client, telemetry, updater, credential
reader, repository-content scanner, or transcript writer. Previews, the viewer,
and the usage panel display data locally without saving it. Pane output may
contain anything already printed by the program in that pane; use `Ctrl-P` or
`agmux --no-preview` to hide previews.

## Execution and controls

- tmux commands use argument arrays. Agent startup uses a small Python runner
  with literal arguments; it does not evaluate a shell command string. A JSON
  argument preserves shell metacharacters and tmux command separators as data.
  When the agent exits, the runner records the outcome and then executes your
  regular login shell.
- Displayed text is filtered for terminal control characters before any of
  agmux's own styling is applied.
- New session and window names reject control characters and tmux's unsupported
  dot/colon separators. Session operations target resolved session IDs.
- The popup passes its originating client explicitly; command-line switching
  refuses ambiguous shared-client contexts.
- **Deletion** (`agmux delete`/`kill`, picker `Ctrl-D`/`Delete`) is destructive
  but always explicitly user-triggered: it targets exactly one resolved session,
  prints the target's details, and requires typing the session name (or, for the
  CLI, the explicit single-session `--yes` flag). There is no bulk, wildcard, or
  automatic deletion. Immediately before killing, the resolved session ID is
  re-checked against the confirmed snapshot, so a renamed or replaced session,
  or one whose panes or clients changed meanwhile, is refused rather than
  destroyed, and asks you to confirm again. agmux does not change tmux's global
  `detach-on-destroy` and only ever issues `kill-session` for the single
  confirmed target.
- Adding a shell window uses `new-window -d`, so no attached client is moved and
  no input is injected into any existing pane.
- The read-only viewer captures output without attaching a tmux client or
  sending keys to the pane.
- Entering a session cancels tmux copy-mode on its panes with `send-keys -X
  cancel`, which exits the mode without typing into the running program.
- Preview and viewer output is parsed for SGR styling only. agmux reconstructs
  a fixed, canonical set of color/attribute codes from the parsed styles and
  never writes a pane's original escape bytes to the terminal.

## Notifications

Completion notifications are best effort and off the critical path. agmux may
run `notify-send` locally when a session bus is present, or a command you supply
via `AGMUX_NOTIFY_CMD`. That command is split with shell-like quoting and
executed directly with `subprocess.Popen` as an argv list — no shell is
involved — so placeholders are substituted as data. agmux sends no data over the
network itself.

While an agent started by agmux runs, a small daemon thread polls its pane and
raises the same notification if it looks blocked on a prompt; `agmux watch` does
the same for all sessions from the command line. These checks only read the
visible screen (via `capture-pane`) and never send input to a pane.

## Color

Optional color uses fixed ANSI style codes applied only to already-filtered
text and only when stdout is an interactive terminal (or, for the curses UI,
when the terminal supports it). `NO_COLOR`, `TERM=dumb`, and piped output
disable it. Color codes carry no data or behavior; pane output is shown with
terminal defaults and is never passed through raw escape sequences.

## Verification

- Source review: only standard-library imports; no `shell=True`, no shell
  command launcher, no HTTP or socket client, no production file writes.
- Isolated tmux integration tests (`python3 -m unittest discover -s tests`)
  cover literal argument forwarding including `$()`, backticks, quotes,
  newlines, a standalone `;`, and a tmux format string; exact-name selection;
  working directory and active-pane metadata; bounded tmux command waits;
  deletion confirmation and cancellation; exact-target deletion leaving other
  sessions intact; refusal when the target changed during confirmation;
  shell-window working-directory and detached behavior; agent detection from a
  foreground command; per-session CPU/memory sampling (Linux and BSD `ps`
  forms); previews and state reads ending on the newest output while a pane is
  scrolled in copy mode; tracked agent-pane state after a window switch;
  multi-root repository listing; Codex and OpenCode usage-log/database parsing
  including older OpenCode schemas; notification command execution; and plain
  piped output.
- Private-terminal checks (`python3 tests/ui_smoke.py`) drive navigation,
  rename, attach/detach, the new-session wizard with a fake agent binary,
  help/usage/preview toggles, no input forwarding from the viewer, switching
  only the originating terminal with two clients attached, the picker's
  delete-confirmation/cancellation flow, and the detached shell-window action.
- Both suites use only a private tmux socket (`AGMUX_TMUX_SOCKET`) and private
  pseudo-terminals. They do not delete, rename, attach to, detach, or send input
  to any real user session. The only `kill-server` calls target the private test
  socket.

This document covers this version of agmux. It does not replace a review of your
agent executables, shell startup files, or tmux configuration and hooks. Those
programs keep their normal behavior and access when launched.
