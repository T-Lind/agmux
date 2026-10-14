# Changelog

All notable changes to agmux are documented here. The format is loosely based
on [Keep a Changelog](https://keepachangelog.com/), and the project follows
[semantic versioning](https://semver.org/).

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

[0.5.0]: https://github.com/T-Lind/agmux/compare/v0.4.0...v0.5.0
