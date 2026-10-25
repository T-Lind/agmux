# Changelog

All notable changes to agmux are documented here. The format is loosely based
on [Keep a Changelog](https://keepachangelog.com/), and the project follows
[semantic versioning](https://semver.org/).

## [0.6.0]

### Added
- `agmux info SESSION [--json]`: agent, state, directory, panes, attached
  client ttys, and CPU/memory for one session.
- `agmux usage --json` for scripts and dashboards, alongside the existing
  `list --json`.
- `agmux list --no-resources` for a list without the process sample.
- Agent-pane tracking: state detection and prompt notifications keep watching
  the pane agmux launched even after you switch to another window in the same
  session.
- Chrome trimming for Claude Code, Gemini, Aider, Crush, and Goose previews, in
  addition to Codex and OpenCode.
- More prompt/working heuristics for Claude/Gemini/Aider forms such as
  `(Y)es/(N)o`, "Apply this change?", and "Waiting for your approval".
- A scrollable help panel on short terminals, an `Other directory` option in
  the new-session wizard, compact headers on narrow terminals, and client
  counts when more than one terminal is attached.
- `agmux doctor` now reports the tmux socket/version and popup support, Python
  and config file details, actually readable Codex/OpenCode usage sources,
  ASCII mode, and how many sessions need input or have finished.

### Changed
- OpenCode usage tolerates both `session` and `session_v2` schemas, includes
  reasoning/cache tokens when recorded, normalizes second/millisecond
  timestamps, and shows session age.
- CPU/memory sampling falls back to the BSD/macOS `ps -A -o` form and builds
  the process tree once per refresh.
- `humanize_tokens` keeps one decimal below 10K, `money` prints `$0` for zero,
  and the home directory is shown as `~`.

### Fixed
- Attaching to a finished agent now clears the `✓` badge (`mark_read` moves the
  recorded state from finished to idle) instead of leaving it forever.
- The blocked-prompt watcher now re-notifies when the same prompt appears again
  after the agent resumes; previously it remembered the prompt too long.
- State reads capture the end of the pane history, so a pane left scrolled in
  copy mode cannot hide a current prompt.
- Attaching no longer turns an exited agent into a perpetual "running" row.
- Global `--no-notify` before `new`/`watch`, and `--no-resources` after `list`,
  are parsed correctly.
- `install.sh --uninstall` also removes the installed man page, and a
  non-writable man directory no longer aborts the rest of the install.

## [0.5.1]

### Changed
- Label the per-session resource figures explicitly as `cpu X%` and `mem Y`
  everywhere (picker, `agmux list`, screenshot), so they read as machine CPU and
  RAM rather than token, context-window, or rate-limit usage. Help text and the
  README say so outright.

## [0.5.0]

### Added
- Per-session CPU and memory in the picker, `agmux list`, and `agmux doctor`,
  summed across every pane and its child processes. Hidden with
  `AGMUX_RESOURCES=0` or `--no-resources`.
- `agmux list --json` for scripting, with agent, status, directory, CPU, and
  memory per session.
- Optional config file (`~/.config/agmux/config`, or `$AGMUX_CONFIG`) so the
  same settings and project roots can follow you between machines. Environment
  variables override it.
- Multiple project roots: `AGMUX_REPOS` now accepts a `:`-separated list.
- A friendlier `install.sh` (links the command, wires up `~/.bashrc` and
  `~/.tmux.conf` idempotently, installs a man page, supports `--uninstall`),
  a `Makefile`, and an `agmux(1)` man page.

### Changed
- Previews and the read-only viewer now pin to the end of the pane's history and
  hug the bottom of their area, so they always show the newest output even when
  a pane has been scrolled up in copy mode.

### Fixed
- The launch/notification unit test no longer fails when the suite is run from
  inside a real tmux session.

## [0.4.0]
- Fix chrome trimming, auto-expanding preview, OpenCode usage.

## [0.3.0]
- Colored previews, agent-state detection, chrome trimming.

## [0.2.0]
- Agent-aware tmux session manager.

[0.6.0]: https://github.com/T-Lind/agmux/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/T-Lind/agmux/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/T-Lind/agmux/compare/v0.4.0...v0.5.0
