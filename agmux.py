#!/usr/bin/env python3
"""agmux - a dependency-free tmux session manager for AI coding agents.

One standard-library Python file. agmux lists and manages tmux sessions, starts
agent sessions (Codex, OpenCode, Claude Code, ...) or plain shells, flags
sessions whose agent has finished, and shows what local provider usage data it
can find. The optional Discord notifier can deliver alerts to your webhook.
"""

import argparse
import base64
import contextlib
import curses
import fcntl
import hashlib
import json
import locale
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unicodedata
import uuid

try:
    import sqlite3
except ImportError:  # pragma: no cover - sqlite3 is in the standard library
    sqlite3 = None


APP = "agmux"
VERSION = "0.9.2"
SELF = Path(__file__).resolve()


def _env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


# Machine-local defaults from an optional config file, so the same settings can
# follow you across machines (via dotfiles) without exporting variables by hand.
# Environment variables always win over the file, and the file is optional.
_CONFIG_KEYS = {
    "persist": "AGMUX_PERSIST",
    "state_dir": "AGMUX_STATE_DIR",
    "save_interval": "AGMUX_SAVE_INTERVAL",
    "repos": "AGMUX_REPOS",
    "agent": "AGMUX_AGENT",
    "default_agent": "AGMUX_AGENT",
    "tmux_socket": "AGMUX_TMUX_SOCKET",
    "notify": "AGMUX_NOTIFY",
    "notify_cmd": "AGMUX_NOTIFY_CMD",
    "watch_interval": "AGMUX_WATCH_INTERVAL",
    "blocked_patterns": "AGMUX_BLOCKED_PATTERNS",
    "working_patterns": "AGMUX_WORKING_PATTERNS",
    "trim_chrome": "AGMUX_TRIM_CHROME",
    "follow_preview": "AGMUX_FOLLOW_PREVIEW",
    "resources": "AGMUX_RESOURCES",
    "ascii": "AGMUX_ASCII",
    "bin": "AGMUX_BIN",
    "codex_home": "CODEX_HOME",
    "opencode_data_dir": "OPENCODE_DATA_DIR",
}


def config_path():
    """Where agmux looks for its optional ``key = value`` config file."""
    explicit = os.environ.get("AGMUX_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP / "config"


def load_config(path=None):
    """Apply defaults from the config file, if present.

    Values are written into the environment only when the matching variable is
    not already set, so the shell (and CI) always override the file. A line is
    a comment when it starts with ``#``; blank lines and unknown keys are
    ignored. Returns the settings applied.
    """
    source = Path(path) if path is not None else config_path()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return {}
    applied = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env_name = _CONFIG_KEYS.get(key.strip().lower().replace("-", "_"))
        if not env_name:
            continue
        raw_value = value.strip()
        value = raw_value
        # Unwrap a quoted config value, preserving quotes around individual
        # arguments in commands such as: agmux discord -- "{title}" "{session}".
        if (len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]
                and (env_name != "AGMUX_NOTIFY_CMD" or value[0] not in value[1:-1])):
            value = value[1:-1]
        if env_name in os.environ:
            inherited = os.environ[env_name]
            # tmux sessions launched by 0.9.1 can still export the broken
            # value. Recover only the exact invalid form of this config line.
            if (env_name != "AGMUX_NOTIFY_CMD" or inherited == value
                    or inherited != raw_value.strip("'\"")):
                continue
            try:
                shlex.split(inherited)
            except ValueError:
                pass
            else:
                continue
        os.environ[env_name] = value
        applied[env_name] = os.environ[env_name]
    return applied


CONFIG_APPLIED = load_config()

CODEX_HOME = Path(_env("CODEX_HOME", default=str(Path.home() / ".codex")))
OPENCODE_HOME = Path(_env("OPENCODE_DATA_DIR", default=str(
    Path(_env("XDG_DATA_HOME", default=str(Path.home() / ".local/share"))) / "opencode")))
OPENCODE_DB = OPENCODE_HOME / "opencode.db"
DEFAULT_AGENT = _env("AGMUX_AGENT", default="codex")


class Error(Exception):
    pass


def clean(text):
    """Keep tmux names and pane output from injecting terminal controls."""
    return "".join(c if c.isprintable() else " " for c in str(text))


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------


class Agent:
    """A known coding agent (or the plain shell)."""

    __slots__ = ("key", "label", "binary", "accent")

    def __init__(self, key, label, binary, accent="default"):
        self.key = key
        self.label = label
        self.binary = binary
        self.accent = accent

    @property
    def is_shell(self):
        return self.binary is None


AGENTS = {}
for _agent in (
    Agent("codex", "Codex", "codex", "agent"),
    Agent("opencode", "OpenCode", "opencode", "secondary"),
    Agent("claude", "Claude", "claude", "warning"),
    Agent("gemini", "Gemini", "gemini", "attached"),
    Agent("aider", "Aider", "aider", "attached"),
    Agent("crush", "Crush", "crush", "secondary"),
    Agent("goose", "Goose", "goose", "agent"),
    Agent("shell", "Shell", None, "muted"),
):
    AGENTS[_agent.key] = _agent

_BY_BINARY = {a.binary: a for a in AGENTS.values() if a.binary}
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "nu",
                  "elvish", "pwsh"}


def detect_agent(process, explicit=None):
    """Resolve an agent from its tmux metadata or foreground command name."""
    if explicit and explicit in AGENTS:
        return AGENTS[explicit]
    if process:
        return _BY_BINARY.get(Path(process).name)
    return None


# --------------------------------------------------------------------------
# Palette (CLI) and glyphs
# --------------------------------------------------------------------------

ANSI = {
    "heading": "\033[36m",    # cyan
    "agent": "\033[35m",      # magenta
    "secondary": "\033[34m",  # blue
    "attached": "\033[32m",   # green
    "finished": "\033[33m",   # yellow
    "muted": "\033[2m",       # dim
    "warning": "\033[31m",    # red
    "reset": "\033[0m",
}


def color_enabled(stream=None):
    """True only for a real interactive terminal that welcomes color."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def color_role(role, enabled):
    """ANSI prefix for a role, or empty string when color is disabled."""
    if not enabled:
        return ""
    code = ANSI.get(role)
    return code if code and role != "default" else ""


def paint(text, role, enabled):
    """Clean then optionally style a fragment; plain text when disabled."""
    text = clean(text)
    prefix = color_role(role, enabled)
    return f"{prefix}{text}{ANSI['reset']}" if prefix else text


def paint_segments(segments, enabled):
    return "".join(paint(text, role, enabled) for text, role in segments)


_ASCII_GLYPHS = {"running": ">", "finished": "*", "blocked": "!", "idle": ".",
                 "shell": ".", "bar_full": "#", "bar_empty": "-", "sep": "-",
                 "dot": "*", "mid": " | ", "ellipsis": "..."}


def _pick_glyphs():
    if os.environ.get("AGMUX_ASCII"):
        return dict(_ASCII_GLYPHS)
    encoding = sys.stdout.encoding or "utf-8"
    try:
        "\u25b6\u2713\u00b7\u2588\u2591\u2500\u25cf\u203a\u2026".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return dict(_ASCII_GLYPHS)
    return {"running": "\u25b6", "finished": "\u2713", "blocked": "!",
            "idle": "\u203a", "shell": "\u00b7", "bar_full": "\u2588",
            "bar_empty": "\u2591", "sep": "\u2500", "dot": "\u25cf",
            "mid": " \u00b7 ", "ellipsis": "\u2026"}


GLYPH = _pick_glyphs()


# --------------------------------------------------------------------------
# ANSI parsing and screen classification
# --------------------------------------------------------------------------

# A parsed style is a tuple: (fg, bg, bold, dim, italic, underline, reverse),
# where fg/bg are xterm-256 color indices or -1 for the terminal default.
DEFAULT_SGR = (-1, -1, 0, 0, 0, 0, 0)

_CUBE = (0, 95, 135, 175, 215, 255)
_BASE_RGB = ((0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
             (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229))


def _rgb_to_xterm(r, g, b):
    ri = min(range(6), key=lambda i: abs(_CUBE[i] - r))
    gi = min(range(6), key=lambda i: abs(_CUBE[i] - g))
    bi = min(range(6), key=lambda i: abs(_CUBE[i] - b))
    cube = 16 + 36 * ri + 6 * gi + bi
    cube_rgb = (_CUBE[ri], _CUBE[gi], _CUBE[bi])
    average = (r + g + b) / 3
    gray_i = max(0, min(23, int(round((average - 8) / 10))))
    gray = 8 + 10 * gray_i
    cube_dist = sum((a - b) ** 2 for a, b in zip((r, g, b), cube_rgb))
    gray_dist = sum((v - gray) ** 2 for v in (r, g, b))
    return 232 + gray_i if gray_dist < cube_dist else cube


def _xterm_to_rgb(index):
    if 16 <= index <= 231:
        index -= 16
        return (_CUBE[index // 36], _CUBE[(index % 36) // 6], _CUBE[index % 6])
    if 232 <= index <= 255:
        value = 8 + 10 * (index - 232)
        return (value, value, value)
    if 0 <= index < 8:
        return _BASE_RGB[index]
    if 8 <= index < 16:
        return _BASE_RGB[index - 8]
    return (0, 0, 0)


def _nearest_base_color(r, g, b):
    return min(range(8), key=lambda i: sum((a - c) ** 2
                                           for a, c in zip((r, g, b), _BASE_RGB[i])))


def _apply_sgr(style, params):
    codes = [int(part) if part else 0 for part in params.split(";")] if params else [0]
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            style[:] = list(DEFAULT_SGR)
        elif code == 1:
            style[2] = 1
        elif code == 2:
            style[3] = 1
        elif code == 3:
            style[4] = 1
        elif code == 4:
            style[5] = 1
        elif code == 7:
            style[6] = 1
        elif code == 22:
            style[2] = style[3] = 0
        elif code == 23:
            style[4] = 0
        elif code == 24:
            style[5] = 0
        elif code == 27:
            style[6] = 0
        elif 30 <= code <= 37:
            style[0] = code - 30
        elif code == 39:
            style[0] = -1
        elif 40 <= code <= 47:
            style[1] = code - 40
        elif code == 49:
            style[1] = -1
        elif 90 <= code <= 97:
            style[0] = code - 90 + 8
        elif 100 <= code <= 107:
            style[1] = code - 100 + 8
        elif code in (38, 48):
            target = 0 if code == 38 else 1
            if index + 1 < len(codes) and codes[index + 1] == 5 and index + 2 < len(codes):
                style[target] = max(0, min(255, codes[index + 2]))
                index += 2
            elif index + 1 < len(codes) and codes[index + 1] == 2 and index + 4 < len(codes):
                style[target] = _rgb_to_xterm(codes[index + 2], codes[index + 3],
                                              codes[index + 4])
                index += 4
        index += 1


def parse_sgr(line):
    """Split a captured line into (text, style) runs.

    Only SGR styling is interpreted; every other escape sequence is consumed
    and discarded, and control characters become spaces, so untrusted pane
    output can never reach the terminal as raw bytes.
    """
    runs, buf, style = [], [], list(DEFAULT_SGR)

    def flush():
        if buf:
            runs.append(("".join(buf), tuple(style)))
            buf.clear()

    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\x1b" and i + 1 < n and line[i + 1] == "[":
            flush()
            j = i + 2
            while j < n and not ("@" <= line[j] <= "~"):
                j += 1
            if j < n:
                if line[j] == "m":
                    _apply_sgr(style, line[i + 2:j])
                i = j + 1
            else:
                i = n
            continue
        if ch == "\x1b" and i + 1 < n and line[i + 1] == "]":
            flush()
            j = i + 2
            while j < n:
                if line[j] == "\x07":
                    j += 1
                    break
                if line[j] == "\x1b" and j + 1 < n and line[j + 1] == "\\":
                    j += 2
                    break
                j += 1
            i = j
            continue
        if ch == "\x1b":
            flush()
            i += 2
            continue
        buf.append(ch if ch.isprintable() else " " if ch != "\t" else " ")
        i += 1
    flush()
    return runs


def _sgr_prefix(style):
    """Canonical, safe SGR prefix (reset + recognized attributes only)."""
    fg, bg, bold, dim, italic, underline, reverse = style
    parts = ["0"]
    if bold:
        parts.append("1")
    if dim:
        parts.append("2")
    if italic:
        parts.append("3")
    if underline:
        parts.append("4")
    if reverse:
        parts.append("7")
    if fg >= 0:
        parts.append(f"38;5;{fg}")
    if bg >= 0:
        parts.append(f"48;5;{bg}")
    return "\x1b[" + ";".join(parts) + "m"


def render_ansi(lines, color):
    """Plain text or safe ANSI reconstruction of captured lines."""
    output = []
    for line in lines:
        runs = parse_sgr(line)
        if not color:
            output.append("".join(text for text, _ in runs))
        else:
            body = "".join(_sgr_prefix(style) + text for text, style in runs)
            output.append(body + "\x1b[0m")
    return output


class AnsiRenderer:
    """Maps parsed SGR styles onto curses attributes and cached color pairs."""

    def __init__(self):
        self.colors = curses.COLORS if curses.has_colors() else 0
        self.pairs = getattr(curses, "COLOR_PAIRS", 0)
        self.enabled = (self.colors >= 8 and not os.environ.get("NO_COLOR"))
        self._pairs = {}
        # Pairs 1-7 are reserved by setup_styles(); start our own well above them.
        self._next = 8

    def _clamp(self, index):
        if self.colors >= 256:
            return max(0, min(self.colors - 1, index))
        if index < 8:
            return index
        if index < 16:
            return index - 8
        return _nearest_base_color(*_xterm_to_rgb(index))

    def _pair(self, fg, bg):
        key = (fg, bg)
        if key in self._pairs:
            return self._pairs[key]
        if self._next >= self.pairs:
            return 0
        try:
            a = self._clamp(fg) if fg >= 0 else -1
            b = self._clamp(bg) if bg >= 0 else -1
            curses.init_pair(self._next, a, b)
            self._pairs[key] = self._next
            self._next += 1
        except curses.error:
            return 0
        return self._pairs[key]

    def attr(self, style):
        fg, bg, bold, dim, italic, underline, reverse = style
        attr = 0
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        if underline:
            attr |= curses.A_UNDERLINE
        if reverse:
            attr |= curses.A_REVERSE
        if not self.enabled or (fg < 0 and bg < 0):
            return attr
        return attr | curses.color_pair(self._pair(fg, bg))


# Screen-state heuristics. These are intentionally conservative and can be
# extended without touching the code: AGMUX_BLOCKED_PATTERNS and
# AGMUX_WORKING_PATTERNS are '|'-joined regular expressions appended here.
# Strong patterns are specific prompt shapes (y/n tokens, numbered Yes/No
# options, "press enter to confirm"). Weak patterns are common English that can
# also appear in an agent's ordinary prose, so they are ignored when an idle
# composer is drawn below them or when the line is clearly assistant output.
_BLOCKED_STRONG = [
    r"\[y/n\]", r"\(y/n\)", r"\[Y/n\]", r"\[y/N\]", r"\byes/no\b",
    r"\(y\)es\s*/\s*\(n\)o", r"\[(?:y/n/a|a/y/n|y/n/a)\]",
    r"\bpress (enter|y)\b[^.]*\b(confirm|continue|approve|allow|deny)",
    r"\bwaiting for (?:your |user )?(?:input|approval|confirmation|permission)\b",
    r"^\s*(?:[>❯›●]|\d+[.)])\s*(?:yes|allow|approve|deny|no)\b",
    r"\b(?:apply|accept|reject)\b[^.?]{0,60}\b(?:change|patch|diff|edit|hunk)s?\?",
]
_BLOCKED_WEAK = [
    r"\ballow\b[^.]*\b(run|command|access|edit|write|execute|this|once|always)\b",
    r"\b(approve|approval|permit|permission)\b",
    r"\bwould you like to\b", r"\bdo you want to\b", r"\bare you sure\b",
    r"\bproceed\?", r"\bcontinue\?", r"\boverwrite\?", r"\bconfirm\?",
    r"\bescalat", r"\bneeds? (your )?(input|approval|permission)\b",
    r"\b(would|do|does|shall|should|can|could|may)\b[^?]{0,120}\?\s*$",
]
_BLOCKED_PATTERNS = _BLOCKED_STRONG + _BLOCKED_WEAK

# Per-agent dialog fingerprints, taken from each harness's own approval,
# question, and trust screens. They are checked first and win even over a
# "esc to interrupt" footer (Codex shows that footer on its question screens).
_AGENT_BLOCKED = {
    "codex": [
        r"would you like to (?:run|make|grant|allow|apply)\b",
        r"press enter to (?:confirm|continue)", r"enter to submit (?:answer|all)",
        r"no, and tell codex what to do differently",
        r"^\s*[›>]?\s*\d+\.\s*yes,? (?:proceed|continue|and don.t ask)",
        r"allow codex to\b", r"do you trust the (?:contents|files)",
    ],
    "claude": [
        r"do you want to (?:proceed|make this edit|create|overwrite|allow|run|apply)\b",
        r"would you like to proceed\?", r"do you trust the files",
        r"^\s*[❯>]?\s*\d+\.\s*yes\b", r"^\s*[❯>]?\s*\d+\.\s*(?:type something|chat about this)",
        r"esc to cancel\s*[·|-]\s*tab to amend", r"claude needs your permission",
        r"waiting for (?:permission|user)",
    ],
    "opencode": [
        r"permission required", r"allow once", r"always allow",
        r"^\s*[┃│|]?\s*reject\s*$", r"enter submit\s+esc dismiss",
    ],
    "gemini": [
        r"allow execution of", r"allow once", r"allow for this session",
        r"waiting for (?:user )?confirmation", r"apply this change\?",
        r"^\s*[●>]?\s*\d+\.\s*(?:yes, allow|allow once|modify with)",
    ],
    "aider": [
        r"\((?:y)\)es\s*/\s*\((?:n)\)o", r"\[(?:yes|no)\]:\s*$",
    ],
    "crush": [
        r"permission required", r"allow for session", r"^\s*(?:allow|deny)\s*$",
    ],
    "goose": [
        r"goose would like to call", r"always allow", r"do you allow",
    ],
}
_IDLE_COMPOSER = re.compile(
    r"^\s*[│┃|]?\s*[›❯>](?!\s*\d+[.)])(?:\s|$)|aider>|\? for shortcuts|"
    r"ctrl\+p commands|ask codex|type your message|esc to interrupt$", re.IGNORECASE)
_PROSE_MARKER = re.compile(r"^\s*[•⏺◦└⎿✻·]\s")
_WORKING_PATTERNS = [
    r"esc to interrupt", r"\besc\b[^.]*\binterrupt\b", r"ctrl\+c to (cancel|stop|interrupt)",
    r"press esc[^.]*\b(stop|cancel|interrupt)\b", r"working\s*(?:\(|•|\.\.\.)",
    r"thinking\s*(?:\(|•|\.\.\.)", r"esc replaces", r"\(esc to cancel,\s*\d+",
    r"\b(?:generating|searching|reading|running|building|analyzing)\b[^.]{0,80}"
    r"\b(?:esc|ctrl\+c)\b[^.]{0,40}\b(?:cancel|stop|interrupt)\b",
]

_PATTERN_CACHE = {}


def _compile_patterns(defaults, extra_env):
    extra = os.environ.get(extra_env) if extra_env else None
    key = (tuple(defaults), extra_env, extra)
    cached = _PATTERN_CACHE.get(key)
    if cached is not None:
        return cached
    patterns = list(defaults)
    if extra:
        patterns.extend(part for part in extra.split("|") if part)
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    _PATTERN_CACHE[key] = compiled
    return compiled


def classify_screen(lines, limit=24, agent=None, title=""):
    """Return (state, prompt) where state is blocked, working, or idle.

    ``prompt`` is the screen line that looked like it needs the user. Passing
    the ``agent`` key enables that harness's own dialog fingerprints; generic
    patterns still apply to every agent.
    """
    tail = [line.rstrip() for line in lines if line.strip()][-limit:]
    working = _compile_patterns(_WORKING_PATTERNS, "AGMUX_WORKING_PATTERNS")
    strong = _compile_patterns(_BLOCKED_STRONG, "AGMUX_BLOCKED_PATTERNS")
    weak = _compile_patterns(_BLOCKED_WEAK, None)
    specific = _compile_patterns(_AGENT_BLOCKED.get(agent or "", []),
                                 None)

    def is_working(line):
        return any(pattern.search(line) for pattern in working)

    if agent == "codex":
        # Use question headers and controls, including free-text and multi-page
        # dialogs. A changing selection/footer is not a new question.
        for index, line in reversed(list(enumerate(tail))):
            if re.search(r"\bshift\s*\+\s*(?:left|←)\s+to answer\b", line, re.I):
                return "blocked", line.strip()[:200]
            if (re.match(r"^\s*[│┃]?\s*Question\s+\d+\s*(?:of|/)\s*\d+\b", line, re.I)
                    and not any("Ask Codex to do anything" in s for s in tail[index + 1:])):
                question = next((s.strip(" │┃") for s in tail[index + 1:]
                                 if "?" in s and not _IDLE_COMPOSER.match(s)), "")
                return "blocked", (question or line.strip())[:200]
        if re.match(r"^\[\s*[!.]\s*\] Action Required\b", title, re.I):
            question = next((s.strip(" │┃") for s in reversed(tail)
                             if "?" in s and not _IDLE_COMPOSER.search(s)), "Codex needs input")
            return "blocked", question[:200]
        if _TITLE_ID.search(title) and not title.startswith("Ready | "):
            if re.match(r"^(?:[⠀-⣿]\s+)?(?:Working|Thinking|Starting|Running)\b", title):
                # Live approval widgets can coexist with a working indicator.
                if not any(re.search(r"enter to (?:confirm|submit)|esc to (?:go back|dismiss)",
                                     s, re.I) for s in tail):
                    return "working", ""
    # 1. This agent's own dialogs: authoritative, and they beat live-work text.
    for line in reversed(tail):
        if any(pattern.search(line) for pattern in specific):
            return "blocked", line.strip()[:200]

    def idle_below(index):
        return any(_IDLE_COMPOSER.search(later) and not is_working(later)
                   for later in tail[index + 1:])

    # 2. Generic patterns, newest line first. A question the assistant wrote in
    # prose, with an idle composer drawn beneath it, is not waiting on anyone.
    for index in range(len(tail) - 1, -1, -1):
        line = tail[index]
        # "Reviewing approval request (10s - esc to interrupt)" is live work.
        if is_working(line):
            continue
        if any(pattern.search(line) for pattern in strong):
            return "blocked", line.strip()[:200]
        if len(tail) - index > 14 or _PROSE_MARKER.match(line) or idle_below(index):
            continue
        if any(pattern.search(line) for pattern in weak):
            return "blocked", line.strip()[:200]
    joined = "\n".join(tail)
    if any(pattern.search(joined) for pattern in working):
        return "working", ""
    return "idle", ""


_BOX_CHARS = set(chr(code) for code in range(0x2500, 0x25A0)) | \
    {" ", ".", "\u00b7", "\u2b1d"}

# Agents draw a fixed input composer and status footer at the bottom of their
# TUIs. It is the same every time and hides real output, so previews drop it.
_CODEX_COMPOSER = re.compile(r"^(?:[\u203a\u276f>]|[\u2502|]\s*[\u203a\u276f>])\s")
_CODEX_STATUS = re.compile(r"\b(?:xhigh|high|medium|low|minimal)\b\s*\u00b7")

# Codex's fullscreen transcript shows this only while the tail is hidden and
# no search/selection/modal owns the view. Do not match prose or bare arrows.
_CODEX_PAUSED_VIEW = re.compile(
    r"(?:(?:New activity|New) · )?↓ (?:Back to bottom|Bottom)(?: · esc)?")


def preview_follow_enabled():
    return os.environ.get("AGMUX_FOLLOW_PREVIEW", "1") != "0"


# Footer text that agents use for their own composer/status chrome. These are
# deliberately specific UI phrases, not generic words that also occur in real
# agent output, and never include live-work lines such as "esc to interrupt".
_CHROME_FOOTER = re.compile(
    r"(?:\? for shortcuts|for shortcuts|accept edits|auto[- ]?accept|"
    r"shift\+tab|ctrl\+p commands|ctrl\+o|ctrl\+t|type your message|"
    r"context left|tokens? (?:used|left)|diffs? ·|quit ·|/help)", re.IGNORECASE)
_CHROME_COMPOSER = re.compile(r"^(?:[\u2502|]\s*)?[\u203a\u276f>]\s")
_AIDER_FOOTER = re.compile(r"^tokens:.*\bcost:", re.IGNORECASE)


def _classify_chrome(agent, line):
    """Return 'strong', 'weak', or None for one trailing preview line."""
    if "\x1b" in line:
        line = "".join(text for text, _ in parse_sgr(line))
    stripped = line.strip()
    if not stripped:
        return "weak"
    if agent == "codex":
        if _CODEX_COMPOSER.match(stripped):
            return "strong"
        if "for shortcuts" in stripped or "for agents" in stripped or "to view" in stripped:
            return "strong"
        if _CODEX_STATUS.search(stripped):
            return "strong"
        if stripped[0] in "\u2514\u2517\u2570":  # tip / corner bullets
            return "weak"
        return None
    if agent == "opencode":
        if "esc interrupt" in stripped or "ctrl+p commands" in stripped:
            return "strong"
        if re.search(r"\b(?:Build|Plan|Agent)\b", stripped) and "\u00b7" in stripped:
            return "weak"
        if all(ch in _BOX_CHARS for ch in stripped):
            return "weak"
        return None
    if agent == "aider":
        if stripped.startswith("aider>") or _AIDER_FOOTER.search(stripped):
            return "strong"
        if all(ch in _BOX_CHARS for ch in stripped):
            return "weak"
        return None
    if agent in ("claude", "gemini", "crush", "goose"):
        if _CHROME_FOOTER.search(stripped) or _CHROME_COMPOSER.match(stripped):
            return "strong"
        if all(ch in _BOX_CHARS for ch in stripped):
            return "weak"
        if stripped[0] in "\u256d\u2570\u2502\u2500\u256e\u256f":
            return "weak"
        return None
    return None


def trim_chrome(agent, lines):
    """Drop a trailing block of agent input/status chrome, if one is present.

    Only a contiguous run of recognized chrome (with at least one strong
    footer/composer line) is removed, so unknown screens and shell sessions are
    left untouched apart from trailing blank lines. Set ``AGMUX_TRIM_CHROME=0``
    to disable trimming.
    """
    if os.environ.get("AGMUX_TRIM_CHROME", "1").strip().lower() in ("0", "false", "no", "off"):
        end = len(lines)
        while end > 0 and not lines[end - 1].strip():
            end -= 1
        return lines[:end]
    end = len(lines)
    strong = False
    while end > 0:
        kind = _classify_chrome(agent, lines[end - 1])
        if kind is None:
            break
        strong = strong or kind == "strong"
        end -= 1
    if strong:
        return lines[:end]
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


# --------------------------------------------------------------------------
# Time and usage helpers
# --------------------------------------------------------------------------


def humanize_delta(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, minutes = seconds // 3600, (seconds % 3600) // 60
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    days, hours = seconds // 86400, (seconds % 86400) // 3600
    return f"{days}d{hours:02d}h" if hours else f"{days}d"


def humanize_age(epoch):
    return humanize_delta(time.time() - epoch) if epoch else ""


def window_label(minutes):
    if not minutes:
        return "?"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def bar_text(percent, width=20):
    percent = max(0.0, min(100.0, float(percent or 0)))
    filled = int(round(percent / 100.0 * width))
    return "[" + GLYPH["bar_full"] * filled + GLYPH["bar_empty"] * (width - filled) + "]"


_USAGE_CACHE = {"at": 0.0, "data": None}


def _last_rate_limits(path, tail_bytes=1_500_000):
    """The most recent codex rate-limit record, read from a file's tail."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if '"rate_limits"' not in line or '"used_percent"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if not limits:
            limits = record.get("rate_limits")
        if isinstance(limits, dict) and limits:
            return limits
    return None


def find_codex_rate_limits():
    sessions = CODEX_HOME / "sessions"
    if not sessions.is_dir():
        return None
    try:
        files = sorted(sessions.rglob("rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:12]
    except OSError:
        return None
    for path in files:
        limits = _last_rate_limits(path)
        if limits:
            return limits
    return None


def codex_usage(max_age=30.0):
    now = time.monotonic()
    if now - _USAGE_CACHE["at"] > max_age:
        _USAGE_CACHE["data"] = find_codex_rate_limits()
        _USAGE_CACHE["at"] = now
    return _USAGE_CACHE["data"]


_OPENCODE_CACHE = {"at": 0.0, "data": None}


def humanize_tokens(count):
    count = int(count or 0)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        value = count / 1_000
        return f"{value:.1f}K" if value < 10 else f"{value:.0f}K"
    return str(count)


def money(value):
    value = float(value or 0)
    if value <= 0:
        return "$0"
    return f"${value:.2f}" if value >= 0.01 else f"${value:.4f}"


def model_label(model):
    """A short provider/model label from OpenCode's stored model JSON."""
    if not model:
        return ""
    try:
        data = json.loads(model) if isinstance(model, str) else model
        name = data.get("id", "")
        provider = data.get("providerID", "")
        if provider and name:
            return f"{provider}/{name}"
        return name or provider or ""
    except (ValueError, AttributeError, TypeError):
        return str(model)[:40]


def _opencode_schema(connection):
    """Find a session table this OpenCode version uses.

    OpenCode has shipped both ``session`` and ``session_v2``; the token columns
    also vary. Detect what is present instead of failing on a schema change.
    """
    for table in ("session_v2", "session"):
        try:
            columns = {row[1] for row in
                       connection.execute(f'pragma table_info("{table}")')}
        except sqlite3.Error:
            continue
        if {"title", "cost", "model", "time_updated"} <= columns:
            return table, columns
    return None, set()


def _to_epoch(value):
    """Normalize a stored timestamp that may be seconds or milliseconds."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number // 1000 if number > 10**11 else number


def opencode_usage(max_age=30.0):
    """Local OpenCode usage (cost and tokens) from its SQLite database.

    OpenCode stores per-session cost and token counts locally. It does not
    expose account rate limits, so this reports the sessions agmux can see.
    Older and newer OpenCode schema names are both supported.
    """
    now = time.monotonic()
    if now - _OPENCODE_CACHE["at"] <= max_age:
        return _OPENCODE_CACHE["data"]
    data = None
    if sqlite3 is not None and OPENCODE_DB.is_file():
        try:
            connection = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=1)
            try:
                table, columns = _opencode_schema(connection)
                if table:
                    expressions = ["title", "cost", "model", "time_updated"]
                    token_columns = ("tokens_input", "tokens_output", "tokens_reasoning",
                                     "tokens_cache_read", "tokens_cache_write")
                    for column in token_columns:
                        expressions.append(column if column in columns else "0")
                    rows = connection.execute(
                        f"select {', '.join(expressions)} from {table} "
                        "order by time_updated desc limit 5").fetchall()
                else:
                    rows = []
            finally:
                connection.close()
            sessions = []
            for row in rows:
                (title, cost, model, updated,
                 tin, tout, reasoning, cache_read, cache_write) = row
                counts = [max(0, int(value or 0))
                          for value in (tin, tout, reasoning, cache_read, cache_write)]
                sessions.append(dict(
                    title=title or "", cost=float(cost or 0),
                    tokens=sum(counts), input=counts[0], output=counts[1],
                    reasoning=counts[2], cache_read=counts[3], cache_write=counts[4],
                    model=model_label(model), updated=_to_epoch(updated)))
            if sessions:
                data = dict(latest=sessions[0], sessions=sessions, table=table,
                            source=str(OPENCODE_DB))
        except (sqlite3.Error, OSError, ValueError, TypeError):
            data = None
    _OPENCODE_CACHE["at"] = now
    _OPENCODE_CACHE["data"] = data
    return data


def opencode_inline(usage=None):
    usage = opencode_usage() if usage is None else usage
    if not usage:
        return ""
    latest = usage["latest"]
    return f"OpenCode {humanize_tokens(latest['tokens'])} tok{GLYPH['mid']}{money(latest['cost'])}"


def usage_inline(limits=None):
    limits = codex_usage() if limits is None else limits
    if not limits:
        return ""
    parts = [AGENTS["codex"].label]
    for key in ("primary", "secondary"):
        window = limits.get(key) or {}
        percent = window.get("used_percent")
        if percent is None:
            continue
        parts.append(f"{window_label(window.get('window_minutes'))} {round(percent):.0f}%")
    return GLYPH["mid"].join(parts) if len(parts) > 1 else ""


def usage_report(width=20, limits=None, opencode=None):
    """(text, role) lines describing locally available usage."""
    limits = codex_usage() if limits is None else limits
    opencode = opencode_usage() if opencode is None else opencode
    lines = []
    if limits:
        lines.append((f"Codex - {limits.get('plan_type') or 'unknown'} plan", "heading"))
        for key in ("primary", "secondary"):
            window = limits.get(key) or {}
            percent = window.get("used_percent")
            if percent is None:
                continue
            label = window_label(window.get("window_minutes"))
            reset = window.get("resets_at")
            tail = f"  resets in {humanize_delta(reset - time.time())}" if reset else ""
            lines.append((f"  {label:>4}  {bar_text(percent, width)} {percent:4.0f}%{tail}",
                          "warning" if percent >= 80 else "default"))
        credits = limits.get("credits") or {}
        if credits.get("has_credits") and credits.get("balance") is not None:
            lines.append((f"  credits: {credits.get('balance')}", "muted"))
        if limits.get("rate_limit_reached_type"):
            lines.append((f"  limit reached: {limits['rate_limit_reached_type']}", "warning"))
    else:
        lines.append(("No local usage data yet - start a Codex session to populate it.", "muted"))

    lines.append(("", "default"))
    lines.append(("OpenCode - local session usage", "heading"))
    if opencode:
        for session in opencode["sessions"][:4]:
            title = clean(session["title"] or "(untitled)")[:40]
            age = humanize_age(session.get("updated"))
            age_text = f"  {age} ago" if age else ""
            lines.append((f"  {title:<42} {humanize_tokens(session['tokens']):>6} tok  "
                          f"{money(session['cost'])}{age_text}", "default"))
        source = opencode.get("table") or "local database"
        lines.append((f"  (OpenCode has no local account rate limits; "
                      f"showing session cost from {source}.)", "muted"))
    else:
        lines.append(("  No local OpenCode usage found.", "muted"))
    return lines


# --------------------------------------------------------------------------
# Per-session resource use (CPU and memory across each pane's process tree)
# --------------------------------------------------------------------------

# One ``ps`` sample serves every session and is reused across a short window so
# a picker refresh does not fork a process per row. Falls back to no data when
# the platform has no compatible ``ps``.
_PS_CACHE = {"at": 0.0, "data": None}


def resources_enabled():
    value = os.environ.get("AGMUX_RESOURCES", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def humanize_size(kilobytes):
    """A compact memory size from a value in kibibytes."""
    value = max(0.0, float(kilobytes or 0))
    if value < 1024:
        return f"{value:.0f}K"
    mb = value / 1024
    if mb < 1024:
        return f"{mb:.0f}M" if mb >= 10 else f"{mb:.1f}M"
    return f"{mb / 1024:.1f}G"


def _process_table(max_age=1.5):
    """pid -> (ppid, cpu_percent, rss_kib), sampled once and cached briefly."""
    now = time.monotonic()
    if _PS_CACHE["data"] is not None and now - _PS_CACHE["at"] <= max_age:
        return _PS_CACHE["data"]
    table = {}
    # Linux accepts ``-e``; BSD/macOS wants ``-A``. Try both so a ps flavour
    # change degrades to a working sample rather than silently losing figures.
    environment = dict(os.environ, LC_ALL="C")
    for args in (["ps", "-eo", "pid=,ppid=,pcpu=,rss="],
                 ["ps", "-A", "-o", "pid=,ppid=,pcpu=,rss="]):
        try:
            result = subprocess.run(args, text=True, errors="replace",
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=4, env=environment)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
        if not result.stdout.strip():
            continue
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 4:
                continue
            try:
                table[int(fields[0])] = (int(fields[1]), float(fields[2]), int(fields[3]))
            except ValueError:
                continue
        if table:
            break
    _PS_CACHE["at"], _PS_CACHE["data"] = now, table
    return table


def _children_map(table):
    children = {}
    for pid, (ppid, _, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    return children


def process_usage(root_pids, table=None, children=None):
    """(cpu_percent, rss_kib) summed over a set of process trees."""
    table = _process_table() if table is None else table
    if not table:
        return 0.0, 0
    if children is None:
        children = _children_map(table)
    cpu = rss = 0.0
    seen = set()
    stack = [pid for pid in root_pids if pid in table]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        _, pcpu, rss_kib = table[pid]
        cpu += pcpu
        rss += rss_kib
        stack.extend(children.get(pid, ()))
    return cpu, int(rss)


def resource_label(cpu, rss):
    """A short, unambiguous ``cpu 12% · mem 340M`` label, or empty.

    The words are deliberate: this is this machine's CPU and memory for the
    session's processes, not token or rate-limit usage.
    """
    parts = []
    if cpu and cpu >= 0.5:
        parts.append(f"cpu {cpu:.0f}%")
    if rss:
        parts.append(f"mem {humanize_size(rss)}")
    return GLYPH["mid"].join(parts)


# --------------------------------------------------------------------------
# Workspace recovery
# --------------------------------------------------------------------------


def recovery_enabled():
    return os.environ.get("AGMUX_PERSIST", "1") != "0"


def state_directory():
    explicit = os.environ.get("AGMUX_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")) / APP


def save_interval():
    try:
        value = float(os.environ.get("AGMUX_SAVE_INTERVAL", "15"))
        return max(2.0, value) if value > 0 and math.isfinite(value) else 0.0
    except ValueError:
        return 15.0


class WorkspaceStore:
    """One private, atomically replaced JSON file per tmux socket.

    The lock covers read/merge/write, including observations from concurrent
    pickers and the background saver. Missing live sessions remain recoverable:
    an empty or newly started server must never erase the previous workspace.
    """

    def __init__(self, socket, directory=None):
        self.socket = socket
        root = Path(directory) if directory is not None else state_directory()
        key = hashlib.sha256(socket.encode()).hexdigest()[:20]
        self.path = root / (key + ".json")

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (ValueError, UnicodeError) as error:
            raise Error(f"Cannot read recovery file {self.path}: {error}. "
                        "Move it aside to start a new snapshot.") from None
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("socket") != self.socket
                or not isinstance(data.get("sessions"), dict)):
            raise Error(f"Unsupported recovery file: {self.path}")
        for name, record in data["sessions"].items():
            validate_workspace(name, record)
        return data["sessions"]

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self.read()

    def update(self, change):
        with self.locked() as records:
            change(records)
            data = dict(version=1, socket=self.socket, sessions=records)
            fd, temporary = tempfile.mkstemp(prefix=".workspace-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                # Make the rename durable as well as the contents.
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return records

    def forget(self, name):
        def remove(records):
            if name not in records:
                raise Error(f"No saved session named {clean(name)!r}.")
            del records[name]
        self.update(remove)


def validate_workspace(name, record):
    """Validate disk data before it can become tmux arguments."""
    def invalid():
        raise Error(f"Invalid saved workspace for {clean(name)!r}.")

    validate_name(name)
    if (not isinstance(record, dict) or not isinstance(record.get("agent"), str)
            or record["agent"] not in AGENTS):
        invalid()
    for key in ("cwd", "server", "sid", "agent_pane", "resume_id"):
        if not isinstance(record.get(key), str) or "\0" in record[key]:
            invalid()
    arguments = record.get("argv")
    if not isinstance(record.get("history_file", ""), str) or "\0" in record.get("history_file", ""):
        invalid()
    if (not isinstance(arguments, list)
            or not all(isinstance(arg, str) and "\0" not in arg for arg in arguments)):
        invalid()
    if record["resume_id"] and not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_-]*", record["resume_id"]):
        invalid()
    if (type(record.get("saved_at")) not in (int, float)
            or not 0 <= record["saved_at"] <= 1e12):
        invalid()
    windows = record.get("windows")
    if not isinstance(windows, list) or not windows or len(windows) > 256:
        invalid()
    indices, pane_ids = set(), set()
    for window in windows:
        if not isinstance(window, dict):
            invalid()
        index = window.get("index")
        if type(index) is not int or not 0 <= index <= 100000 or index in indices:
            invalid()
        indices.add(index)
        for key in ("name", "layout"):
            if not isinstance(window.get(key), str) or "\0" in window[key]:
                invalid()
        if not re.fullmatch(r"[0-9a-fA-F]{4},[0-9x,{}\[\]]+", window["layout"]):
            invalid()
        for key in ("width", "height"):
            if type(window.get(key)) is not int or not 1 <= window[key] <= 10000:
                invalid()
        if not isinstance(window.get("active"), bool) or not isinstance(window.get("zoomed"), bool):
            invalid()
        panes = window.get("panes")
        if not isinstance(panes, list) or not panes or len(panes) > 256:
            invalid()
        for pane in panes:
            if (not isinstance(pane, dict) or not isinstance(pane.get("id"), str)
                    or pane["id"] in pane_ids or not isinstance(pane.get("cwd"), str)
                    or "\0" in pane["cwd"] or not isinstance(pane.get("active"), bool)):
                invalid()
            pane_ids.add(pane["id"])


def explicit_codex_resume(arguments):
    # Never guess from "last" or from another session in the same directory.
    if (len(arguments) >= 2 and arguments[0] == "resume"
            and re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_-]*", arguments[1])):
        return arguments[1]
    return ""


_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_RESUME = {"codex": ["resume"], "claude": ["--resume"], "opencode": ["--session"],
           "gemini": ["--resume"], "crush": ["--session"],
           "goose": ["session", "--resume", "--session-id"]}
# Per-invocation OSC title. Codex renders this independently of terminal width.
# The thread-id item can abbreviate the UUID even in a wide terminal.
_CODEX_TITLE = 'tui.terminal_title=["activity","status","thread-title","thread-id"]'
_TITLE_ID = re.compile(r"(?:^| \| )(" + _UUID +
                       r"|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{2,11}(?:…|\.\.\.))$")


def argument_id(agent, arguments):
    if agent == "codex":
        return explicit_codex_resume(arguments)
    flags = {"claude": ("--session-id", "--resume", "-r"),
             "gemini": ("--resume", "-r", "--session-id"), "goose": ("--session-id",),
             "opencode": ("--session", "-s"), "crush": ("--session", "-s")}
    if "--fork-session" in arguments or "--fork" in arguments:
        return ""
    for index, token in enumerate(arguments):
        if token == "--":
            break
        key, sep, value = token.partition("=")
        if key not in flags.get(agent, ()):
            continue
        if not sep:
            value = arguments[index + 1] if index + 1 < len(arguments) else ""
        pattern = {"claude": _UUID, "gemini": _UUID, "opencode": r"ses_[a-zA-Z0-9]+",
                   "goose": r"[0-9]{8}_[0-9]+", "crush": _UUID}[agent]
        if re.fullmatch(pattern, value):
            return value
    return ""


def launch_arguments(agent, binary, arguments):
    """Keep user argv literal. Only allocate IDs for ordinary new interactive sessions."""
    if agent == "aider" and not arguments and recovery_enabled():
        path = os.environ.get("AIDER_CHAT_HISTORY_FILE")
        if not path:
            directory = state_directory() / "aider-history"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory.resolve() / (str(uuid.uuid4()) + ".md")
        return ["--chat-history-file", str(path)]
    if agent not in ("claude", "gemini", "opencode"):
        return list(arguments)
    # Unknown options and subcommands pass through unchanged. In particular,
    # never allocate a fresh ID for --continue, --resume, or --fork-session.
    values = {"claude": {"--model", "--permission-mode", "--effort", "--agent"},
              "opencode": {"--prompt", "--server"}, "gemini": {"--model", "-m", "-i"}}[agent]
    switches = {"opencode": {"--standalone", "--auto"}}.get(agent, set())
    index = 0
    while index < len(arguments):
        option, equal, _ = arguments[index].partition("=")
        if option in values:
            index += 1 if equal else 2
        elif option in switches:
            index += 1
        else:
            return list(arguments)
    if agent == "claude":
        return ["--session-id", str(uuid.uuid4()), *arguments]
    # Older OpenCode releases accept --session only for an existing conversation.
    try:
        help_text = subprocess.run([binary, "--help"], capture_output=True, text=True,
                                   timeout=3, errors="replace").stdout
        if agent == "gemini" and "--session-id" in help_text:
            return ["--session-id", str(uuid.uuid4()), *arguments]
        if agent == "opencode" and "to create if it does not exist" in help_text:
            return ["--session", "ses_" + uuid.uuid4().hex[:26], *arguments]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return list(arguments)


def read_db(path, sql, args):
    """Short read-only lookup. Unavailable or older schemas leave a fallback path."""
    if sqlite3 is None or not path.is_file():
        return []
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                                uri=True, timeout=0.1)) as db:
            return db.execute(sql, args).fetchall()
    except (sqlite3.Error, OSError):
        return []


def codex_identity(title):
    match = _TITLE_ID.search(title)
    if not match:
        return ""
    prefix = match[1].rstrip(".…")
    if re.fullmatch(_UUID, prefix):
        return prefix
    # A new, unnamed thread also publishes the complete ID in the thread-title field.
    fields = title.split(" | ")
    if len(fields) >= 3 and re.fullmatch(_UUID, fields[-2]) and fields[-2].startswith(prefix):
        return fields[-2]
    db = Path(os.environ.get("CODEX_SQLITE_HOME") or CODEX_HOME) / "state_5.sqlite"
    matches = read_db(db, "SELECT id FROM threads WHERE id GLOB ? LIMIT 2", (prefix + "*",))
    return matches[0][0] if len(matches) == 1 and re.fullmatch(_UUID, matches[0][0]) else ""


def first_message(agent, identity):
    if not identity:
        return ""
    if agent == "codex":
        db = Path(os.environ.get("CODEX_SQLITE_HOME") or CODEX_HOME) / "state_5.sqlite"
        rows = read_db(db, "SELECT first_user_message FROM threads WHERE id=?", (identity,))
        return (rows[0][0] or "") if rows else ""
    if agent == "opencode":
        rows = read_db(OPENCODE_DB, "SELECT json_extract(data,'$.text') FROM session_message "
                       "WHERE session_id=? AND type='user' ORDER BY seq LIMIT 1", (identity,))
        if not rows:
            rows = read_db(OPENCODE_DB, "SELECT json_extract(p.data,'$.text') FROM message m "
                           "JOIN part p ON p.message_id=m.id WHERE m.session_id=? AND "
                           "json_extract(m.data,'$.role')='user' AND json_extract(p.data,'$.type')="
                           "'text' ORDER BY m.time_created,p.id LIMIT 1", (identity,))
        return rows[0][0] if rows and isinstance(rows[0][0], str) else ""
    if agent == "claude" and re.fullmatch(_UUID, identity):
        projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        for path in projects.glob("*/" + identity + ".jsonl"):
            try:
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        item = json.loads(line)
                        if item.get("type") != "user" or item.get("isMeta"):
                            continue
                        content = item.get("message", {}).get("content")
                        if isinstance(content, str):
                            return content
                        if isinstance(content, list):
                            words = [p["text"] for p in content if isinstance(p, dict)
                                     and p.get("type") == "text" and isinstance(p.get("text"), str)]
                            if words:
                                return " ".join(words)
            except (ValueError, AttributeError, OSError):
                pass
    return ""


def codex_turn(identity):
    if not re.fullmatch(_UUID, identity):
        return ()
    root = Path(os.environ.get("CODEX_SQLITE_HOME") or CODEX_HOME)
    db = root / "thread_history_1.sqlite"
    rows = read_db(db, "SELECT t.turn_id,t.status,t.started_at,"
                   "substr(json_extract(i.item_json,'$.text'),-1000) FROM thread_turns t "
                   "LEFT JOIN thread_items i ON i.thread_id=t.thread_id AND i.turn_id=t.turn_id "
                   "AND i.item_id=t.final_agent_item_id WHERE t.thread_id=? "
                   "ORDER BY t.rollout_ordinal DESC LIMIT 1", (identity,))
    if not rows:
        return ()
    turn = rows[0]
    if turn[1] == "completed" and read_db(root / "goals_1.sqlite",
                                          "SELECT 1 FROM thread_goals WHERE thread_id=? "
                                          "AND status='active' LIMIT 1", (identity,)):
        return turn[0], "inProgress", turn[2], ""
    return turn


def agent_state(agent, lines, title, turn=()):
    state, prompt = classify_screen(lines, agent=agent, title=title)
    if agent == "codex" and turn and state != "blocked":
        if turn[1] in ("inProgress", "in_progress"):
            return "working", ""
        if state == "idle" and turn[1] == "completed":
            # A final, direct question is actionable even above an idle composer.
            final = (turn[3] or "").strip()
            if final.endswith("?"):
                return "blocked", clean(final.splitlines()[-1])[-200:]
    if agent == "codex" and state == "idle" and not turn:
        # Older clients lack the turn index. Only the latest assistant block
        # above a composer can be asking for a reply.
        body = [line.strip() for line in trim_chrome("codex", lines) if line.strip()]
        body = [line for line in body if not re.match(r"^Worked for \d", line)]
        if body and body[-1].endswith("?") and any(_CODEX_COMPOSER.match(s.strip()) for s in lines):
            marker = next((s[0] for s in reversed(body) if s.startswith(("• ", "› "))), "")
            if marker == "•":
                return "blocked", body[-1].lstrip("• ")[:200]
    return state, prompt


def recovery_launch(record, fresh=False):
    if fresh:
        return record["agent"], record["argv"]
    agent = record["agent"]
    if agent == "aider":
        if record.get("history_file"):
            return agent, ["--chat-history-file", record["history_file"], "--restore-chat-history"]
        for i, arg in enumerate(record["argv"]):
            if arg == "--":
                break
            flag, sep, path = arg.partition("=")
            if flag == "--chat-history-file":
                if not sep and i + 1 < len(record["argv"]):
                    path = record["argv"][i + 1]
                if path and not path.startswith("-"):
                    return agent, ["--chat-history-file", path, "--restore-chat-history"]
    identity = record["resume_id"] or argument_id(agent, record["argv"])
    if identity and agent in _RESUME:
        args = [*_RESUME[agent], identity]
        # A server or data-directory selector is part of the conversation address.
        locations = {"codex": {"--remote", "--remote-auth-token-env", "--cd", "-C", "--profile", "-p"},
                     "opencode": {"--server"}, "crush": {"--data-dir", "--cwd"}}.get(agent, set())
        standalone = {"codex": "--no-daemon", "opencode": "--standalone"}.get(agent)
        tokens = iter(record["argv"])
        for token in tokens:
            if token == "--":
                break
            key, sep, value = token.partition("=")
            if token == standalone:
                args.append(token)
            elif key in locations:
                value = value if sep else next(tokens, "")
                if value and not value.startswith("-"):
                    args.extend([key, value])
            elif key in ("--prompt", "--model", "-m", "--config", "-c") and not sep:
                next(tokens, None)
        return agent, args
    if agent in ("codex", "claude"):
        return agent, list(_RESUME[agent])
    return "shell", []


def saved_rows(tmux, explicit=False):
    if not explicit and not recovery_enabled():
        return []
    live = {row["name"] for row in tmux.sessions()}
    rows = []
    for name, record in tmux.workspace_store().read().items():
        if name not in live:
            launcher, arguments = recovery_launch(record)
            rows.append(dict(id="saved:" + name, name=name, agent=record["agent"],
                             status="saved", activity=record["saved_at"],
                             windows=len(record["windows"]), attached=0, cwd=record["cwd"],
                             resume_id=record["resume_id"] or argument_id(record["agent"], record["argv"]),
                             resumes_chat=launcher != "shell" and len(arguments) > 1,
                             saved=True, process="", unread=0, cpu=0, rss=0))
    return sorted(rows, key=lambda row: (-row["activity"], row["name"]))


def workspace_saver(socket, directory, server):
    """A single local saver per server; exits when that server stops.

    No shell, service installation, network access, or terminal is involved.
    A separate lifetime lock prevents concurrent invocations spawning savers.
    """
    tmux = Tmux(socket)
    tmux._store = WorkspaceStore(socket, directory)
    path = tmux.workspace_store().path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(server.encode()).hexdigest()[:12]
    fd = os.open(str(path) + "." + key + ".saver.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        interval = save_interval()
        while recovery_enabled() and interval:
            try:
                if tmux.server_identity() != server:
                    break
                if tmux.run("show-options", "-gqv", "@agmux_persist") == "0":
                    break
                tmux.checkpoint()
            except (Error, OSError):
                # Preserve the previous snapshot on transient read/write errors.
                pass
            time.sleep(interval)
    return 0


# --------------------------------------------------------------------------
# tmux
# --------------------------------------------------------------------------

_SESSION_FIELDS = ("session_id", "session_name", "session_windows", "session_attached",
                   "session_activity", "session_created",
                   "@agmux_agent", "@agmux_status", "@agmux_unread", "@agmux_finished",
                   "@agmux_pane", "@agmux_exit", "@agmux_done_turn")


class Tmux:
    def __init__(self, socket=None):
        socket = socket or _env("AGMUX_TMUX_SOCKET", "CX_TMUX_SOCKET")
        if not socket and os.environ.get("TMUX"):
            socket = os.environ["TMUX"].split(",", 1)[0]
        executable = shutil.which("tmux")
        if not executable:
            raise Error("tmux is not installed.")
        self.command = [executable] + (["-S", socket] if socket else [])
        self.socket = socket or str(Path(os.environ.get("TMUX_TMPDIR", "/tmp")) /
                                    f"tmux-{os.getuid()}" / "default")
        self._store = None
        self._saver_started = False
        self.recovery_error = ""

    def run(self, *args, optional=False):
        try:
            result = subprocess.run(self.command + list(args), text=True, errors="replace",
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        except subprocess.TimeoutExpired:
            raise Error("tmux did not respond within five seconds.") from None
        if result.returncode:
            missing = ("no server running", "no sessions", "no such file or directory",
                       "connection refused", "server exited unexpectedly")
            if optional and any(s in result.stderr.lower() for s in missing):
                return ""
            raise Error(result.stderr.strip() or "tmux command failed")
        return result.stdout.rstrip("\n")

    def workspace_store(self):
        if self._store is None:
            actual = self.run("-N", "display-message", "-p", "#{socket_path}", optional=True)
            self.socket = actual or self.socket
            self._store = WorkspaceStore(self.socket)
        return self._store

    def server_identity(self):
        return self.run("-N", "display-message", "-p", "#{pid}:#{start_time}", optional=True)

    def checkpoint(self):
        """Merge live topology into disk state without forgetting closed sessions."""
        def collect(records):
            server = self.server_identity()
            if not server:
                return
            rows = self.sessions()
            metadata = {}
            fmt = ("#{session_id}\t#{@agmux_argv}\t#{@agmux_resume_id}\t#{@agmux_launch_cwd}"
                   "\t#{@agmux_auto_name}\t#{@agmux_history_file}")
            for line in self.run("list-sessions", "-F", fmt, optional=True).splitlines():
                fields = line.split("\t", 5)
                if len(fields) == 6:
                    metadata[fields[0]] = fields[1:]
            fields = ("session_id", "window_index", "window_name", "window_layout",
                      "window_width", "window_height", "window_active", "window_zoomed_flag",
                      "pane_id", "pane_current_path", "pane_active", "pane_title")
            fmt = "\t".join("#{" + field + "}" for field in fields)
            windows, titles = {}, {}
            for line in self.run("list-panes", "-a", "-F", fmt, optional=True).splitlines():
                values = line.split("\t", len(fields) - 1)
                if len(values) != len(fields):
                    raise Error("Cannot save workspace: unsupported control characters in tmux metadata.")
                sid, index, name, layout, width, height, active, zoomed, pane, cwd, focus, title = values
                titles[pane] = title
                group = windows.setdefault(sid, {})
                window = group.setdefault(int(index), dict(
                    index=int(index), name=name, layout=layout, width=int(width), height=int(height),
                    active=active == "1", zoomed=zoomed == "1", panes=[]))
                window["panes"].append(dict(id=pane, cwd=cwd, active=focus == "1"))
            for row in rows:
                topology = sorted(windows.get(row["id"], {}).values(), key=lambda w: w["index"])
                if not topology:
                    continue
                # tmux may have changed between queries. Never save an incomplete session.
                if len(topology) != row["windows"]:
                    continue
                raw, resume_id, launch_cwd, auto_name, history_file = metadata.get(
                    row["id"], ("[]", "", "", "", ""))
                if row["agent"] == "codex":
                    resume_id = codex_identity(titles.get(row["agent_pane"], "")) or resume_id
                try:
                    argv = json.loads(raw or "[]")
                except ValueError:
                    argv = []
                cwd = next((p["cwd"] for w in topology if w["active"]
                            for p in w["panes"] if p["active"]), topology[0]["panes"][0]["cwd"])
                record = dict(agent=row["agent"] if row["agent"] in AGENTS else "shell",
                              cwd=(launch_cwd or cwd) if row["agent"] != "shell" else cwd,
                              argv=argv, resume_id=resume_id, auto_name=auto_name == row["name"],
                              agent_pane=row.get("agent_pane") or "", windows=topology,
                              sid=row["id"], server=server, saved_at=time.time())
                if history_file:
                    record["history_file"] = history_file
                try:
                    validate_workspace(row["name"], record)
                except Error:
                    continue  # externally created names/paths may not be representable
                # A rename should move the definition, not leave a phantom saved row.
                for old in list(records):
                    previous = records[old]
                    if (old != row["name"] and previous["server"] == server
                            and previous["sid"] == row["id"]):
                        del records[old]
                records[row["name"]] = record
        return self.workspace_store().update(collect)

    def autosave(self):
        try:
            if not recovery_enabled() or not save_interval():
                if self.server_identity():
                    self.run("set-option", "-g", "@agmux_persist", "0")
                if not recovery_enabled():
                    return
            self.checkpoint()
            self.start_saver()
            self.recovery_error = ""
        except (Error, OSError) as error:
            self.recovery_error = f"Workspace recovery unavailable: {clean(error)}"

    def start_saver(self):
        if self._saver_started or not save_interval():
            return
        server = self.server_identity()
        if not server or not self.sessions():
            return
        store = self.workspace_store()
        self.run("set-option", "-g", "@agmux_persist", "1")
        # Do not rely on the server's inherited environment or on shell quoting.
        worker = subprocess.Popen([sys.executable, str(SELF), "_save-loop", "--socket", self.socket,
                          "--state-dir", str(store.path.parent), "--server", server],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        threading.Thread(target=worker.wait, daemon=True).start()
        self._saver_started = True

    def restore(self, name, fresh=False, notify=True):
        if any(row["name"] == name for row in self.sessions()):
            raise Error(f"Session {clean(name)!r} is already running.")
        store = self.workspace_store()
        restored = []

        def rebuild(records):
            if name not in records:
                raise Error(f"No saved session named {clean(name)!r}.")
            record = records[name]
            validate_workspace(name, record)
            for window in record["windows"]:
                for pane in window["panes"]:
                    if not Path(pane["cwd"]).is_dir():
                        raise Error(f"Saved directory does not exist: {clean(pane['cwd'])}")
            agent, arguments = recovery_launch(record, fresh)
            profile = AGENTS[agent]
            if not profile.is_shell and not shutil.which(profile.binary):
                raise Error(f"{profile.binary} is not on PATH. The saved workspace was kept.")
            if not profile.is_shell and not Path(record["cwd"]).is_dir():
                raise Error(f"Saved agent directory does not exist: {clean(record['cwd'])}")
            if fresh and not profile.is_shell:
                arguments = launch_arguments(agent, shutil.which(profile.binary), arguments)
            if (agent == "aider" and not fresh
                    and not (Path(record["cwd"]) / Path(arguments[1]).expanduser()).is_file()):
                raise Error(f"Saved Aider history file is missing: {clean(arguments[1])}")
            shell = os.environ.get("SHELL", "/bin/bash")
            if not Path(shell).is_file() or not os.access(shell, os.X_OK):
                shell = "/bin/bash"
            first = record["windows"][0]
            # Allow enough temporary room for every split before applying saved layouts.
            width = max(200, first["width"])
            height = max(80, max(len(w["panes"]) * 3 for w in record["windows"]))
            created = self.run("new-session", "-d", "-s", name, "-n", first["name"],
                               "-c", first["panes"][0]["cwd"], "-x", str(width), "-y", str(height),
                               "-P", "-F", "#{session_id}\t#{pane_id}\t#{window_index}",
                               shell, "-l")
            sid, first_pane, initial_index = created.split("\t")
            try:
                if int(initial_index) != first["index"]:
                    self.run("move-window", "-s", first_pane, "-t", f"{sid}:{first['index']}")
                panes = {}
                active_window = f"{sid}:{first['index']}"
                for index, window in enumerate(record["windows"]):
                    if index == 0:
                        pane = first_pane
                    else:
                        pane = self.run("new-window", "-d", "-t", f"{sid}:{window['index']}",
                                        "-n", window["name"], "-c", window["panes"][0]["cwd"],
                                        "-P", "-F", "#{pane_id}", shell, "-l")
                    target = f"{sid}:{window['index']}"
                    panes[window["panes"][0]["id"]] = pane
                    for saved in window["panes"][1:]:
                        new_pane = self.run("split-window", "-d", "-t", pane, "-l", "1",
                                            "-c", saved["cwd"], "-P", "-F", "#{pane_id}",
                                            shell, "-l")
                        panes[saved["id"]] = new_pane
                    self.run("select-layout", "-t", target, window["layout"])
                    focused = next((p for p in window["panes"] if p["active"]), window["panes"][0])
                    self.run("select-pane", "-t", panes[focused["id"]])
                    if window["zoomed"]:
                        self.run("resize-pane", "-Z", "-t", panes[focused["id"]])
                    if window["active"]:
                        active_window = target
                self.run("select-window", "-t", active_window)
                tracked = panes.get(record["agent_pane"], first_pane)
                self.session_option(sid, "agmux_agent", record["agent"])
                self.session_option(sid, "agmux_status", "shell" if profile.is_shell else "running")
                self.session_option(sid, "agmux_pane", tracked)
                self.session_option(sid, "agmux_argv", json.dumps(record["argv"]))
                identity = argument_id(agent, arguments) or ("" if fresh else record["resume_id"])
                self.session_option(sid, "agmux_resume_id", identity)
                if agent == "aider" and arguments[:1] == ["--chat-history-file"] and len(arguments) > 1:
                    self.session_option(sid, "agmux_history_file", arguments[1])
                if record.get("auto_name") is True:
                    self.session_option(sid, "agmux_auto_name", name)
                self.session_option(sid, "agmux_launch_cwd", record["cwd"])
                if not profile.is_shell:
                    self.run("respawn-pane", "-k", "-t", tracked, "-c", record["cwd"],
                             *self.agent_environment(), sys.executable, str(SELF), "_launch", "--agent", agent,
                             "--bin", shutil.which(profile.binary), "--shell", shell,
                             "--session", name, "--notify" if notify else "--no-notify",
                             "--argv", json.dumps(arguments))
                restored.append(sid)
            except BaseException:
                try:
                    self.run("kill-session", "-t", sid, optional=True)
                except Error:
                    pass
                raise
        with store.locked() as records:
            rebuild(records)
        self.autosave()
        return restored[0]

    def sessions(self):
        fmt = "\t".join("#{" + key + "}" for key in _SESSION_FIELDS)
        rows = []
        for line in self.run("list-sessions", "-F", fmt, optional=True).splitlines():
            fields = line.split("\t")
            if len(fields) != len(_SESSION_FIELDS):
                continue
            (sid, name, windows, attached, activity, created,
             agent, status, unread, finished, agent_pane, exit_code, done_turn) = fields
            rows.append(dict(id=sid, name=name, windows=int(windows), attached=int(attached),
                             activity=int(activity or 0), created=int(created or 0),
                             agent=agent or "", status=status or "",
                             unread=int(unread or 0), finished=int(finished or 0),
                             agent_pane=agent_pane or "", exit_code=exit_code, done_turn=done_turn))
        return sorted(rows, key=lambda row: (-row["activity"], row["name"]))

    def session_details(self, detect=False, resources=False):
        rows = self.sessions()
        if not rows:
            return []
        fmt = "\t".join("#{" + key + "}" for key in
                        ("session_id", "window_active", "pane_active",
                         "pane_current_command", "pane_current_path", "pane_id", "pane_title",
                         "@agmux_resume_id"))
        panes, active = {}, {}
        for line in self.run("list-panes", "-a", "-F", fmt, optional=True).splitlines():
            fields = line.split("\t")
            if len(fields) != 8:
                continue
            session_id, window_active, pane_active, process, cwd, pane, title, identity = fields
            entry = dict(session=session_id, process=process, cwd=cwd, pane=pane,
                         title=title, identity=identity,
                         active=window_active == "1" and pane_active == "1")
            panes[pane] = entry
            if entry["active"]:
                active[session_id] = entry
        usage = self.session_resources() if resources else {}
        for row in rows:
            info = active.get(row["id"], {"process": "", "cwd": "", "pane": ""})
            row["process"] = info["process"]
            row["cwd"] = info["cwd"]
            row["pane"] = info["pane"]
            row["state"] = ""
            row["prompt"] = ""
            row["cpu"], row["rss"] = usage.get(row["id"], (0.0, 0))
            if not row["agent"]:
                detected = detect_agent(row["process"])
                row["agent"] = detected.key if detected else ""
            # Prefer the pane where agmux launched the agent, so switching to a
            # shell window in the same session does not hide a waiting prompt.
            state = panes.get(row.get("agent_pane") or "")
            if not state or state["session"] != row["id"]:
                state = panes.get(row["pane"])
            if (detect and row["agent"] and row["agent"] != "shell" and state
                    and (state["process"] not in SHELL_COMMANDS
                         or (row["status"] == "running" and state["pane"] == row["agent_pane"]))):
                try:
                    turn = () if row["agent"] != "codex" else codex_turn(
                        codex_identity(state["title"]) or
                        (state["identity"] if row["status"] == "running" else ""))
                    state_name, prompt = agent_state(row["agent"], self.visible_lines(state["pane"]),
                                                     state["title"], turn)
                    if turn:
                        row["native_turn"] = turn[0]
                        if turn[1] == "completed":
                            row["completed_turn"] = turn[0]
                            if state_name == "idle" and state["title"].startswith("Ready | "):
                                row["ready_turn"] = turn[0]
                    if (row["status"] == "running" and row["done_turn"] and
                            (not turn or turn[:2] != (row["done_turn"], "completed"))):
                        row["unread"] = 0
                except Error:
                    state_name, prompt = "", ""
                row["state"], row["prompt"] = state_name, prompt
        return rows

    def pane_pids(self):
        """session_id -> list of pane process ids, from one tmux query."""
        pids = {}
        fmt = "#{session_id}\t#{pane_pid}"
        for line in self.run("list-panes", "-a", "-F", fmt, optional=True).splitlines():
            session_id, _, pid = line.partition("\t")
            try:
                pids.setdefault(session_id, []).append(int(pid))
            except ValueError:
                continue
        return pids

    def session_resources(self):
        """session_id -> (cpu_percent, rss_kib), summed over each pane's tree."""
        table = _process_table()
        if not table:
            return {}
        children = _children_map(table)
        return {sid: process_usage(pids, table, children)
                for sid, pids in self.pane_pids().items()}

    def pane_lines(self, target, lines=24):
        """Visible screen text of a pane, pinned to its newest output.

        ``-E -`` means classification never reads a stale scrolled-back view
        when a user has left the pane in copy mode.
        """
        return self.run("capture-pane", "-p", "-t", target, "-S", f"-{lines}",
                        "-E", "-").splitlines()

    def visible_lines(self, pane):
        return self.run("capture-pane", "-p", "-t", pane, "-S", "0", "-E", "-").splitlines()

    def resolve(self, target):
        rows = self.sessions()
        # A literal name such as "$3" takes precedence over another session's ID.
        for row in rows:
            if target == row["name"]:
                return row
        for row in rows:
            if target == row["id"]:
                return row
        raise Error(f"No tmux session named {clean(target)!r}. Try: {APP}")

    def client(self, explicit=None):
        if explicit or not os.environ.get("TMUX"):
            return explicit
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            raise Error(f"Cannot identify this terminal. Open the picker with Ctrl-b then Ctrl-s.")
        sid = self.run("display-message", "-p", "-t", pane, "#{session_id}")
        matches = []
        for line in self.run("list-clients", "-F", "#{client_tty}\t#{session_id}").splitlines():
            tty, _, session_id = line.partition("\t")
            if session_id == sid:
                matches.append(tty)
        if len(matches) != 1:
            raise Error("Multiple or no clients share this session. Use Ctrl-b then Ctrl-s from your terminal.")
        return matches[0]

    def current_session_id(self):
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            raise Error("Cannot identify the current tmux session.")
        return self.run("display-message", "-p", "-t", pane, "#{session_id}")

    def follow_preview(self, sid, client=None):
        """Return a supported paused chat to latest when selected in the picker.

        This is deliberately separate from preview(), peek, and screen/state
        reads: those stay read-only. Codex's fullscreen transcript owns Ctrl-End
        when its paused-view control is shown (verified in codex-cli 0.166.0).
        Never guess another agent's keys or disturb another attached reader.
        """
        if not preview_follow_enabled():
            return False
        fmt = "\t".join("#{" + key + "}" for key in
                        ("pane_id", "window_active", "pane_active", "pane_current_command",
                         "@agmux_agent", "pane_in_mode", "alternate_on"))
        panes = [line.split("\t") for line in
                 self.run("list-panes", "-s", "-t", sid, "-F", fmt).splitlines()]
        active = next((p for p in panes if len(p) == 7 and p[1:3] == ["1", "1"]), None)
        if not active or active[5:] != ["0", "1"]:
            return False
        pane, _, _, command, explicit, _, _ = active
        agent = detect_agent(command, explicit)
        # Python is the foreground process group for agmux's own agent runner.
        binary = Path(command).name
        if (not agent or agent.key != "codex"
                or not (binary in ("codex", "python", "python3") or binary.startswith("codex-"))):
            return False
        clients = self.run("list-clients", "-t", sid, "-F", "#{client_tty}", optional=True)
        if any(tty != client for tty in clients.splitlines()):
            return False
        screen = self.run("capture-pane", "-p", "-t", pane, "-S", "0", "-E", "-")
        if not any(_CODEX_PAUSED_VIEW.fullmatch(line.strip()) for line in screen.splitlines()[-24:]):
            return False
        self.run("send-keys", "-t", pane, "C-End")
        return True

    def preview(self, sid, height=16, ansi=False):
        fmt = "\t".join("#{" + key + "}" for key in
                        ("pane_id", "window_active", "pane_active",
                         "pane_current_command", "pane_current_path"))
        lines = self.run("list-panes", "-s", "-t", sid, "-F", fmt).splitlines()
        panes = [line.split("\t", 4) for line in lines]
        active = next((p for p in panes if len(p) == 5 and p[1:3] == ["1", "1"]), None)
        if not active:
            return ["No active pane"]
        # Capture extra rows so trimming the agent's input/status footer still
        # leaves a full preview of real content. ``-E -`` pins the range to the
        # end of the pane's history, so previews always end on the newest output
        # even if a client has scrolled the pane into copy mode.
        depth = height + 12
        output = self.run("capture-pane", "-p", *(["-e"] if ansi else []),
                          "-t", active[0], "-S", f"-{depth}", "-E", "-")
        return [f"{active[3]}  |  {active[4]}", ""] + output.splitlines()[-depth:]


    def connect(self, sid, client=None):
        self.mark_read(sid)
        self.cancel_modes(sid)
        client = self.client(client)
        if os.environ.get("TMUX") or client:
            args = ["switch-client", "-t", sid]
            if client:
                args += ["-c", client]
            self.run(*args)
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise Error(f"Attaching requires a terminal. Use {APP} list for text output.")
            return subprocess.call(self.command + ["attach-session", "-t", sid])
        return 0

    def session_option(self, sid, name, value):
        self.run("set-option", "-t", sid, "@" + name, str(value))

    def agent_environment(self):
        # An existing tmux server may have been started from another shell.
        values = {"AGMUX_STATE_DIR": str(state_directory().resolve()),
                  "AGMUX_PERSIST": "1" if recovery_enabled() else "0"}
        for key in ("AGMUX_CONFIG", "XDG_CONFIG_HOME", "AGMUX_NOTIFY_CMD", "AGMUX_WATCH_INTERVAL",
                    "AGMUX_BLOCKED_PATTERNS", "AGMUX_WORKING_PATTERNS", "CODEX_HOME",
                    "CODEX_SQLITE_HOME", "OPENCODE_DATA_DIR", "CLAUDE_CONFIG_DIR"):
            if key in os.environ:
                values[key] = os.environ[key]
        return [arg for key, value in values.items() for arg in ("-e", f"{key}={value}")]

    def mark_read(self, sid):
        """Clear the unread/finished badge once the user has looked at a session.

        The agent wrapper records ``finished`` so the picker can raise a check
        mark. Attaching means the user has seen it, so also move the state on to
        ``idle``; otherwise the check mark would stay forever.
        """
        try:
            self.session_option(sid, "agmux_unread", "0")
            if any(row["id"] == sid and row["status"] in ("finished", "exited")
                   for row in self.sessions()):
                self.session_option(sid, "agmux_status", "idle")
        except Error:
            pass

    def cancel_modes(self, sid):
        """Leave copy-mode (or similar) so a switch/attach lands on the live view.

        Uses ``send-keys -X cancel``, which exits the mode without typing into
        the running program. Panes that are not in a mode are left untouched.
        """
        fmt = "#{pane_id}\t#{pane_in_mode}"
        for line in self.run("list-panes", "-s", "-t", sid, "-F", fmt,
                             optional=True).splitlines():
            pane, _, mode = line.partition("\t")
            if mode == "1":
                self.run("send-keys", "-t", pane, "-X", "cancel", optional=True)

    def create(self, name, cwd, agent=DEFAULT_AGENT, agent_args=(), notify=True, auto_name=False,
               remember_agent=False):
        validate_name(name)
        profile = AGENTS.get(agent)
        if profile is None:
            raise Error(f"Unknown agent {clean(agent)!r}. Known: {', '.join(AGENTS)}")
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise Error(f"Directory does not exist: {directory}")
        if any(row["name"] == name for row in self.sessions()):
            raise Error(f"Session {name!r} already exists. Use {APP} attach {shlex.quote(name)}")
        args = ["new-session", "-d", "-s", name, "-c", str(directory),
                "-P", "-F", "#{session_id}"]
        actual = agent_args
        if not profile.is_shell:
            binary = shutil.which(profile.binary)
            if not binary:
                raise Error(f"{profile.binary} is not on PATH. Use --agent shell for a plain session.")
            actual = launch_arguments(agent, binary, agent_args)
            user_shell = os.environ.get("SHELL", "/bin/bash")
            if not Path(user_shell).is_file() or not os.access(user_shell, os.X_OK):
                user_shell = "/bin/bash"
            # tmux executes this argv directly. JSON is one literal argument, even
            # when agent arguments contain tmux separators or shell metacharacters.
            args += [*self.agent_environment(), sys.executable, str(SELF), "_launch",
                     "--agent", profile.key, "--bin", binary, "--shell", user_shell,
                     "--session", name, "--notify" if notify else "--no-notify",
                     "--argv", json.dumps(list(actual))]
        # Seed pre-upgrade usage before the new session is visible to the saver.
        project_history(self)
        sid = self.run(*args)
        project_history(self, cwd=str(directory), selected_agent=agent if remember_agent else None)
        try:
            self.session_option(sid, "agmux_agent", profile.key)
            if profile.is_shell:
                self.session_option(sid, "agmux_status", "shell")
            if auto_name and not profile.is_shell:
                self.session_option(sid, "agmux_auto_name", name)
            self.session_option(sid, "agmux_started", str(int(time.time())))
            self.session_option(sid, "agmux_argv", json.dumps(list(agent_args)))
            self.session_option(sid, "agmux_launch_cwd", str(directory))
            resume_id = argument_id(agent, actual)
            if resume_id:
                self.session_option(sid, "agmux_resume_id", resume_id)
            if agent == "aider" and not agent_args and actual:
                self.session_option(sid, "agmux_history_file", actual[1])
        except Error:
            pass
        self.autosave()
        return sid

    def rename(self, sid, name):
        validate_name(name)
        self.run("rename-session", "-t", sid, name)
        self.session_option(sid, "agmux_auto_name", "")
        self.autosave()

    def suggested_name(self, cwd, agent):
        rows = self.sessions()
        if recovery_enabled():
            try:
                rows.extend({"name": name} for name in self.workspace_store().read())
            except (Error, OSError):
                pass  # The caller can still create a live session when saving is unavailable.
        return default_name(cwd, rows, agent)

    def name_from_message(self, sid, placeholder, message):
        # Target the original name so a concurrent manual rename wins.
        printable = clean(message[:256]).translate(str.maketrans(".:;#\\", "     "))
        base = " ".join(printable.split())[:48].rstrip()
        if not base:
            return False
        rows = self.sessions()
        if not any(r["id"] == sid and r["name"] == placeholder for r in rows):
            return False
        used = {r["name"] for r in rows}
        if recovery_enabled():
            used.update(self.workspace_store().read())
        name, suffix = base, 2
        while name in used and name != placeholder:
            name, suffix = f"{base} {suffix}", suffix + 1
        self.run("rename-session", "-t", "=" + placeholder, "--", name)
        self.session_option(sid, "agmux_auto_name", "")
        return True

    def active_pane(self, sid):
        """Command and working directory of a session's current pane."""
        fmt = "\t".join("#{" + key + "}" for key in
                        ("window_active", "pane_active", "pane_id",
                         "pane_current_command", "pane_current_path"))
        for line in self.run("list-panes", "-s", "-t", sid, "-F", fmt).splitlines():
            fields = line.split("\t", 4)
            if len(fields) == 5 and fields[0] == "1" and fields[1] == "1":
                return dict(pane=fields[2], command=fields[3], path=fields[4])
        return None

    def describe(self, sid):
        """A snapshot of one session: windows, panes, clients, and paths."""
        row = next((r for r in self.sessions() if r["id"] == sid), None)
        if row is None:
            raise Error("That session no longer exists.")
        fmt = "\t".join("#{" + key + "}" for key in
                        ("session_id", "window_index", "pane_id", "pane_current_command",
                         "pane_current_path", "window_active", "pane_active"))
        panes = []
        for line in self.run("list-panes", "-a", "-F", fmt, optional=True).splitlines():
            fields = line.split("\t", 6)
            if len(fields) == 7 and fields[0] == sid:
                panes.append(dict(window=int(fields[1]), pane=fields[2], command=fields[3],
                                  path=fields[4],
                                  active=fields[5] == "1" and fields[6] == "1"))
        clients = []
        for line in self.run("list-clients", "-F", "#{client_tty}\t#{session_id}",
                             optional=True).splitlines():
            tty, _, session_id = line.partition("\t")
            if tty and session_id == sid:
                clients.append(tty)
        info = dict(row)
        info["panes"] = panes
        info["pane_count"] = len(panes)
        info["clients"] = clients
        info["client_count"] = len(clients)
        return info

    def delete(self, sid, expected=None):
        """Revalidate immediately, then kill exactly one resolved session."""
        self.autosave()
        info = self.describe(sid)
        if expected is not None and session_fingerprint(info) != session_fingerprint(expected):
            raise Error(
                "Session changed during confirmation "
                f"(now {clean(info['name'])!r}, {info['windows']} window(s), "
                f"{info['pane_count']} pane(s), {info['client_count']} client(s)). "
                "Nothing was deleted. Start the confirmation again.")
        self.run("kill-session", "-t", sid)

    def shell_window(self, sid, cwd=None, name=None):
        """Create an ordinary shell window, detached, in the active pane's directory."""
        if cwd is not None:
            directory = Path(cwd).expanduser().resolve()
        else:
            pane = self.active_pane(sid)
            directory = Path(pane["path"]) if pane and pane["path"] else Path.home()
        if not directory.is_dir():
            raise Error(f"Directory does not exist: {directory}")
        args = ["new-window", "-d", "-t", sid, "-c", str(directory)]
        if name:
            validate_window_name(name)
            args += ["-n", name]
        args += ["-P", "-F", "#{window_index}"]
        index = self.run(*args)
        self.autosave()
        return index


def session_fingerprint(info):
    """Fields whose change must invalidate an in-progress delete confirmation."""
    return (info["id"], info["name"], info["windows"], info["pane_count"], info["client_count"])


def validate_name(name):
    if not name.strip() or any(c in ".:" or not c.isprintable() for c in name):
        raise Error("Use a nonempty session name without dots, colons, or control characters.")


def validate_window_name(name):
    if not name.strip() or any(not c.isprintable() for c in name):
        raise Error("Use a nonempty, printable window name.")
    if len(name) > 60:
        raise Error("Use a window name of 60 characters or fewer.")


def fuzzy(query, value):
    chars = iter(value.casefold())
    return all(any(c == needle for c in chars) for needle in query.casefold())


def searchable(row):
    agent = AGENTS.get(row.get("agent") or "")
    return " ".join(str(row.get(key, "")) for key in
                    ("name", "cwd", "process", "status", "state", "prompt")) + \
        (" " + agent.label if agent else "")


def repos_roots():
    """The configured project roots, newest syntax: one or more directories.

    ``AGMUX_REPOS`` may list several roots separated by ``os.pathsep`` (``:``
    on Linux); ``~`` is expanded. Falls back to ``~/source/repos``.
    """
    raw = _env("AGMUX_REPOS", "CX_REPOS", default=str(Path.home() / "source" / "repos"))
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        path = Path(part).expanduser()
        if path not in roots:
            roots.append(path)
    return roots


def repos():
    found, seen = [], set()
    roots = repos_roots()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_dir():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(dict(id=str(path), name=path.name))
    if not found and not any(root.is_dir() for root in roots):
        raise Error("No repository directory exists: "
                    f"{', '.join(str(root) for root in roots)}. Set AGMUX_REPOS to change it.")
    return sorted(found, key=lambda repo: repo["name"].lower())


def short_path(directory):
    directory = str(directory or "")
    home = str(Path.home())
    if directory == home:
        return "~"
    prefix = home + os.sep
    return "~/" + directory[len(prefix):] if directory.startswith(prefix) else directory


def default_name(directory, sessions, agent=DEFAULT_AGENT):
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in Path(directory).name)
    prefix = "codex" if agent == "codex" else ("sh" if agent == "shell" else agent)
    base = f"{prefix}-" + (slug or "session")
    names = {s["name"] for s in sessions}
    candidate, index = base, 2
    while candidate in names:
        candidate, index = f"{base}-{index}", index + 1
    return candidate


def row_status(row):
    """blocked, finished, running, idle, or shell.

    ``blocked`` means the agent looks like it is waiting on the user (a
    permission or confirmation prompt); ``idle`` means it is alive but at its
    prompt rather than working. A tracked agent whose process has exited and
    left a login shell behind is idle rather than perennially running.
    """
    if row.get("saved"):
        return "saved"
    status = row.get("status")
    state = row.get("state")
    agent = row.get("agent")
    if state == "blocked":
        return "blocked"
    if state == "working":
        return "running"
    if status == "exited":
        return "exited"
    if row.get("unread") or status == "finished":
        return "finished"
    if state == "idle":
        return "idle" if agent and agent != "shell" else "shell"
    if status in ("idle", "read"):
        return "idle" if agent and agent != "shell" else "shell"
    if status == "running":
        return "running"
    if agent and agent != "shell":
        if row.get("process") in SHELL_COMMANDS:
            return "idle"
        return "running"
    return "shell"


_STATUS_ORDER = {"blocked": 0, "finished": 1, "running": 2, "idle": 3, "shell": 4, "saved": 5}


def state_priority(row):
    return _STATUS_ORDER.get(row_status(row), 5)


# --------------------------------------------------------------------------
# Notifications and the agent launch wrapper
# --------------------------------------------------------------------------


def notify_default():
    value = os.environ.get("AGMUX_NOTIFY", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def notification_argv(command, title, body, session, exit_code):
    return [part.format(title=title, body=body, session=session, exit_code=exit_code)
            for part in shlex.split(command)]


def _run_notification(title, body, session, exit_code=-1):
    """Best-effort desktop/terminal notice. Never blocks or raises."""
    title, body, session = clean(title), clean(body), clean(session)
    custom = os.environ.get("AGMUX_NOTIFY_CMD")
    if custom:
        try:
            parts = notification_argv(custom, title, body, session, exit_code)
            if parts[:2] == [APP, "discord"]:
                parts[:1] = [sys.executable, str(SELF)]
            if parts:
                subprocess.Popen(parts, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except (ValueError, OSError, KeyError, IndexError):
            pass
    if shutil.which("notify-send") and _env("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        try:
            subprocess.Popen(["notify-send", "-a", APP, "-u", "normal", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def send_notification(label, session, exit_code, seconds, enabled):
    """Notify that an agent finished."""
    if not enabled:
        return
    outcome = "completed" if exit_code == 0 else f"exited (code {exit_code})"
    _run_notification(f"{label} finished", f"{session}: {outcome} in {humanize_delta(seconds)}",
                      session, exit_code)


def send_blocked_notification(label, session, prompt, enabled=True):
    """Notify that an agent is waiting on the user (permission or question)."""
    if not enabled:
        return
    _run_notification(f"{label} needs input", f"{session}: {prompt}", session)


def watch_interval():
    try:
        interval = float(os.environ.get("AGMUX_WATCH_INTERVAL", "3") or 3)
        return max(1.0, interval) if math.isfinite(interval) else 3.0
    except ValueError:
        return 3.0


def blocked_transition(previous_prompt, state, prompt):
    """Track when a blocked prompt is new.

    Returns ``(prompt_to_notify_or_None, next_previous_prompt)``. Leaving the
    blocked state clears the memory, so the same prompt asks again later and is
    notified again.
    """
    if state != "blocked":
        return None, None
    if prompt and prompt != previous_prompt:
        return prompt, prompt
    return None, previous_prompt


def submitted_screen_message(agent, lines):
    """Fallback for harnesses without a readable message store. Require a later reply
    or a second composer, so the current draft never becomes a session name."""
    prefixes = {"codex": "›", "claude": "❯", "gemini": ">", "aider": ">",
                "opencode": ">", "goose": "❯", "crush": ">"}
    marker = prefixes.get(agent)
    if not marker:
        return ""
    for index, line in enumerate(lines):
        text = line.strip()
        if not text.startswith(marker + " "):
            continue
        prompt = text[2:].strip()
        if not prompt or re.match(r"\d+[.)]\s|Ask Codex\b|Type your message\b", prompt, re.I):
            continue
        later_lines = [later.strip() for later in lines[index + 1:]]
        if any(later == marker or later.startswith(marker + " ") for later in later_lines):
            return prompt
        if agent != "codex" and any(re.match(r"^[•⏺✦]\s", later) for later in later_lines):
            return prompt
    return ""


def monitor_agent(tmux, sid, pane, profile, stop, started, notify, previous_turn=""):
    last_prompt = None
    pending_done, announced = "", previous_turn
    unread = False
    needs_checkpoint = False
    while not stop.is_set():
        try:
            metadata = tmux.run("display-message", "-p", "-t", pane,
                                "#{session_name}\t#{@agmux_auto_name}\t#{@agmux_resume_id}\t#{pane_title}")
            name, placeholder, identity, title = metadata.split("\t", 3)
            lines = tmux.visible_lines(pane)
            current = ""
            if profile.key == "codex":
                current = codex_identity(title)
            elif profile.key == "goose" and not identity:
                banner = re.search(r"(?im)^\s*session id:\s*(\d{8}_\d+)\s*$", "\n".join(lines))
                current = banner[1] if banner else ""
            if current and current != identity:
                identity = current
                tmux.session_option(sid, "agmux_resume_id", identity)
                needs_checkpoint = True
            if placeholder and placeholder == name:
                message = first_message(profile.key, identity)
                if not message:
                    message = submitted_screen_message(profile.key, lines)
                if message and tmux.name_from_message(sid, name, message):
                    needs_checkpoint = True
                    name = tmux.run("display-message", "-p", "-t", pane, "#{session_name}")
            if needs_checkpoint and recovery_enabled():
                tmux.checkpoint()
                needs_checkpoint = False
            turn = codex_turn(identity) if profile.key == "codex" else ()
            state, prompt = agent_state(profile.key, lines, title, turn)
            notice, last_prompt = blocked_transition(last_prompt, state, prompt)
            if notice:
                send_blocked_notification(profile.label, name, notice, notify)
            # A quiet repaint, tool result or interrupted turn cannot finish a chat.
            # Require a completed turn from this launch and two ready samples.
            done = (turn[0] if state == "idle" and title.startswith("Ready | ")
                    and turn and turn[1] == "completed" and isinstance(turn[2], (int, float))
                    and turn[2] >= int(started) else "")
            if done and done == pending_done and done != announced:
                tmux.session_option(sid, "agmux_done_turn", done)
                tmux.session_option(sid, "agmux_unread", "1")
                if notify:
                    _run_notification("Codex turn finished", name, name, 0)
                announced = done
                unread = True
            elif unread and (state != "idle" or (turn and turn[0] != announced)):
                tmux.session_option(sid, "agmux_unread", "0")
                unread = False
            pending_done = done
        except (Error, OSError, ValueError):
            pass
        stop.wait(watch_interval())


def launch_agent(agent_key, executable, shell, session, arguments, notify_enabled):
    """Run an installed agent with literal argv, then leave a login shell open.

    Records running/finished state on the tmux session so the picker can badge
    completions, and fires a best-effort notification when the agent exits.
    """
    profile = AGENTS.get(agent_key) or Agent(agent_key, agent_key.title() or agent_key,
                                             executable, "default")
    tmux = None
    sid = None
    try:
        tmux = Tmux(os.environ.get("TMUX", "").partition(",")[0] or None)
        sid = tmux.current_session_id()
    except Error:
        pass
    pane = os.environ.get("TMUX_PANE") or ""

    def set_option(key, value):
        if tmux and sid:
            try:
                tmux.session_option(sid, key, value)
            except Error:
                pass

    set_option("agmux_agent", profile.key)
    set_option("agmux_status", "running")
    set_option("agmux_started", str(int(time.time())))
    set_option("agmux_unread", "0")
    if pane:
        set_option("agmux_pane", pane)

    started = time.time()
    code = 0
    stop_event = threading.Event()
    watcher = None
    try:
        actual = (["-c", _CODEX_TITLE, *arguments]
                  if tmux and sid and agent_key == "codex" else arguments)
        prior = codex_turn(explicit_codex_resume(arguments)) if agent_key == "codex" else ()
        child = subprocess.Popen([executable, *actual])
        if tmux and sid and pane:
            watcher = threading.Thread(target=monitor_agent, args=(
                tmux, sid, pane, profile, stop_event, started, notify_enabled,
                prior[0] if prior else ""), daemon=True)
            watcher.start()
        while True:
            try:
                child.wait()
                code = child.returncode
                break
            except KeyboardInterrupt:
                # The foreground process group also delivers Ctrl-C to the agent.
                # Keep waiting until it exits before starting the shell.
                continue
    except OSError as error:
        print(f"{APP}: {clean(error)}", file=sys.stderr)
        code = 127
    stop_event.set()
    if watcher:
        watcher.join()
    if tmux and pane and agent_key in ("codex", "opencode", "crush"):
        try:
            output = tmux.run("capture-pane", "-J", "-p", "-t", pane, "-S", "-80", "-E", "-")
            pattern = {"codex": r"To continue this session, run codex resume (" + _UUID + ")",
                       "opencode": r"^\s*opencode (?:-s|--session) (ses_[A-Za-z0-9]+)",
                       "crush": r"\bcrush (?:-s|--session) (" + _UUID + ")"}[agent_key]
            matches = re.findall(pattern + r"(?![a-zA-Z0-9_-])", output, re.M)
            if matches:
                set_option("agmux_resume_id", matches[-1])
        except Error:
            pass

    elapsed = time.time() - started
    set_option("agmux_status", "exited" if agent_key == "codex" else "finished")
    set_option("agmux_exit", str(code))
    set_option("agmux_finished", str(int(time.time())))
    set_option("agmux_duration", str(int(elapsed)))
    set_option("agmux_unread", "1")

    if tmux and pane:
        try:
            session = tmux.run("display-message", "-p", "-t", pane, "#{session_name}") or session
            if recovery_enabled():
                tmux.checkpoint()
        except (Error, OSError):
            pass
    if agent_key == "codex":
        if notify_enabled:
            _run_notification("Codex closed", f"{session} (exit {code})", session, code)
    else:
        send_notification(profile.label, session, code, elapsed, notify_enabled)
    if tmux and sid and notify_enabled:
        try:
            tmux.run("display-message", "-d", "8000",
                     f"{APP}: {profile.label} exited in {session} "
                     f"({humanize_delta(elapsed)})", optional=True)
        except Error:
            pass
    if tmux:
        tmux.autosave()
    os.execv(shell, [shell, "-l"])


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------


class Style:
    """Curses attributes, falling back to monochrome when color is unavailable."""

    def __init__(self):
        self.name = curses.A_BOLD
        self.heading = curses.A_BOLD
        self.agent = curses.A_BOLD
        self.secondary = curses.A_BOLD
        self.attached = curses.A_BOLD
        self.finished = curses.A_BOLD
        self.muted = curses.A_DIM
        self.selected = curses.A_REVERSE
        self.warning = curses.A_BOLD
        self.help = curses.A_DIM
        self.default = 0


def setup_styles():
    """Small restrained palette; degrades gracefully and honors NO_COLOR."""
    if os.environ.get("NO_COLOR"):
        return Style()
    try:
        if not curses.has_colors() or curses.COLORS < 8:
            return Style()
        curses.start_color()
        background = -1
        try:
            curses.use_default_colors()
        except curses.error:
            background = 0
        curses.init_pair(1, curses.COLOR_CYAN, background)      # headings / names
        curses.init_pair(2, curses.COLOR_GREEN, background)     # attached / running
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selection
        curses.init_pair(4, curses.COLOR_RED, background)       # warnings
        curses.init_pair(5, curses.COLOR_MAGENTA, background)   # agent badge
        curses.init_pair(6, curses.COLOR_YELLOW, background)    # finished
        curses.init_pair(7, curses.COLOR_BLUE, background)      # secondary agent
    except curses.error:
        return Style()
    style = Style()
    style.name = curses.color_pair(1) | curses.A_BOLD
    style.heading = curses.color_pair(1) | curses.A_BOLD
    style.agent = curses.color_pair(5) | curses.A_BOLD
    style.secondary = curses.color_pair(7) | curses.A_BOLD
    style.attached = curses.color_pair(2) | curses.A_BOLD
    style.finished = curses.color_pair(6) | curses.A_BOLD
    style.muted = curses.A_DIM
    style.selected = curses.color_pair(3)
    style.warning = curses.color_pair(4) | curses.A_BOLD
    style.help = curses.A_DIM
    style.default = 0
    return style


def put(screen, y, x, value, attr=0):
    height, width = screen.getmaxyx()
    if 0 <= y < height and 0 <= x < width - 1:
        try:
            screen.addnstr(y, x, clean(value), width - x - 1, attr)
        except curses.error:
            pass


def put_right(screen, y, value, attr=0):
    _, width = screen.getmaxyx()
    put(screen, y, max(0, width - len(clean(value)) - 1), value, attr)


def put_runs(screen, y, runs, renderer, x=0, limit=None):
    """Draw parsed (text, style) runs, mapping styles through the renderer."""
    height, width = screen.getmaxyx()
    if not (0 <= y < height):
        return
    edge = width - 1 if limit is None else min(width - 1, limit)
    for text, style in runs:
        if x >= edge:
            break
        text = clean(text)
        if not text:
            continue
        try:
            screen.addnstr(y, x, text, edge - x, renderer.attr(style))
        except curses.error:
            pass
        x += len(text)


def complete_path(value):
    """Tab-complete a directory path: extend to the longest common prefix."""
    expanded = os.path.expanduser(value)
    base, _, partial = expanded.rpartition("/")
    folder = base + "/" if base or expanded.startswith("/") else "./"
    try:
        names = sorted(n for n in os.listdir(folder)
                       if n.startswith(partial) and os.path.isdir(os.path.join(folder, n))
                       and (partial.startswith(".") or not n.startswith(".")))
    except OSError:
        return value
    if not names:
        return value
    common = os.path.commonprefix(names)
    done = (base + "/" if base or expanded.startswith("/") else "") + common
    if len(names) == 1:
        done += "/"
    if value.startswith("~") and expanded != value:
        home = os.path.expanduser("~")
        if done.startswith(home):
            done = "~" + done[len(home):]
    return done if len(done) >= len(value) else value


def prompt(screen, label, initial="", style=None, placeholder="", path=False):
    """One-line editor drawn at the bottom of the screen.

    Starts empty unless ``initial`` is given. ``placeholder`` is shown dimmed
    while the line is empty so Enter-on-blank can mean "use the suggestion".
    Supports cursor movement, Ctrl-W/K/U/A/E, and Tab path completion.
    Returns the text, or None when cancelled.
    """
    style = style or Style()
    value, cursor = initial, len(initial)
    screen.timeout(-1)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    while True:
        height, width = screen.getmaxyx()
        row = max(0, height - 2)
        screen.move(row, 0)
        screen.clrtoeol()
        head = f"{label}: "
        put(screen, row, 0, head, style.heading)
        if value:
            put(screen, row, len(head), value, style.default)
        elif placeholder:
            put(screen, row, len(head), placeholder, style.muted)
        hint = "Enter accept | Esc cancel" + (" | Tab complete" if path else "")
        put(screen, row + 1, 0, hint, style.help)
        try:
            screen.move(row, min(width - 2, len(head) + cursor))
        except curses.error:
            pass
        screen.refresh()
        key = screen.get_wch()
        if key in ("\n", "\r", curses.KEY_ENTER):
            break
        if key in ("\x1b", "\x03"):
            value = None
            break
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if cursor:
                value, cursor = value[:cursor - 1] + value[cursor:], cursor - 1
        elif key == curses.KEY_DC:
            value = value[:cursor] + value[cursor + 1:]
        elif key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(value), cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            cursor = 0
        elif key in (curses.KEY_END, "\x05"):
            cursor = len(value)
        elif key == "\x15":
            value, cursor = value[cursor:], 0
        elif key == "\x0b":
            value = value[:cursor]
        elif key == "\x17":
            head_text = value[:cursor].rstrip()
            cut = max(head_text.rfind(" "), head_text.rfind("/"), -1) + 1
            value, cursor = value[:cut] + value[cursor:], cut
        elif key == "\t" and path:
            completed = complete_path(value[:cursor])
            value, cursor = completed + value[cursor:], len(completed)
        elif isinstance(key, str) and key.isprintable():
            value, cursor = value[:cursor] + key + value[cursor:], cursor + 1
    screen.timeout(200)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    return value


def confirm_key(screen, question, style=None, danger=True):
    """Single-keypress yes/no. Only ``y`` confirms; anything else cancels."""
    style = style or Style()
    screen.timeout(-1)
    height, _ = screen.getmaxyx()
    screen.move(max(0, height - 2), 0)
    screen.clrtoeol()
    put(screen, height - 2, 0, f"{question} [y/N]", style.warning if danger else style.heading)
    screen.refresh()
    try:
        key = screen.get_wch()
    except curses.error:
        key = ""
    screen.timeout(200)
    return key in ("y", "Y")


# --------------------------------------------------------------------------
# Read-only text selection and clipboard
# --------------------------------------------------------------------------


def plain_lines(body):
    return ["".join(text for text, _ in runs) for runs in body]


def selected_text(lines, anchor, cursor):
    """Inclusive character selection, independent of terminal escape styling."""
    if not lines:
        return ""
    start, end = sorted((anchor, cursor))
    first, last = start[0], end[0]
    if first == last:
        return lines[first][start[1]:end[1] + 1]
    return "\n".join([lines[first][start[1]:]] + lines[first + 1:last]
                     + [lines[last][:end[1] + 1]])


def text_cells(value):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in value)


def column_index(value, column):
    cells = 0
    for index, char in enumerate(value):
        cells += text_cells(char)
        if cells > column:
            return index
    return max(0, len(value) - 1)


def copy_text(value, tmux=None):
    """Copy only after an explicit user action; never invoke a shell."""
    if not value:
        raise Error("Select some text first.")
    commands = []
    if os.environ.get("WAYLAND_DISPLAY"):
        commands.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        commands.extend((["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]))
    if sys.platform == "darwin":
        commands.append(["pbcopy"])
    for command in commands:
        executable = shutil.which(command[0])
        if not executable:
            continue
        try:
            result = subprocess.run([executable, *command[1:]], input=value, text=True,
                                    encoding="utf-8", stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=3)
            if result.returncode == 0:
                return f"Copied {len(value)} characters ({command[0]})"
        except (OSError, subprocess.TimeoutExpired):
            continue
    if tmux is not None and os.environ.get("TMUX"):
        try:
            result = subprocess.run(tmux.command + ["load-buffer", "-w", "-"], input=value,
                                    text=True, encoding="utf-8", stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=3)
            if result.returncode == 0:
                return (f"Copied {len(value)} characters to tmux buffer; "
                        "terminal clipboard if enabled")
        except (OSError, subprocess.TimeoutExpired):
            pass
    # OSC 52 works over SSH in supporting terminals; base64 cannot inject controls.
    data = value.encode("utf-8")
    if len(data) > 100000:
        raise Error("Selection exceeds the terminal clipboard limit; use wl-copy or xclip.")
    sequence = b"\033]52;c;" + base64.b64encode(data) + b"\a"
    if os.environ.get("TMUX"):
        # tmux passthrough requires allow-passthrough on in the user's tmux config.
        sequence = b"\033Ptmux;" + sequence.replace(b"\033", b"\033\033") + b"\033\\"
    sys.stdout.flush()
    os.write(sys.stdout.fileno(), sequence)
    return f"Sent {len(value)} characters to terminal clipboard (OSC 52)"


def viewer(screen, tmux, row, style=None):
    """Read and select output; never attach or forward keys to the agent."""
    previous_mouse = None
    try:
        previous_mouse = curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)[1]
        curses.mouseinterval(0)
    except curses.error:
        pass
    try:
        return output_viewer(screen, tmux, row, style)
    finally:
        if previous_mouse is not None:
            curses.mousemask(previous_mouse)


def output_viewer(screen, tmux, row, style=None):
    style = style or Style()
    renderer = AnsiRenderer()
    offset, refreshed, body, caption = 0, 0.0, [], ""
    anchor, cursor, dragging = None, None, False
    message, alert = "", False
    screen.timeout(200)
    screen.keypad(True)
    while True:
        height, width = screen.getmaxyx()
        now = time.monotonic()
        # Freeze the captured output while selecting so new agent output cannot
        # move the selected text underneath the cursor.
        if anchor is None and now - refreshed > 2:
            try:
                lines = tmux.preview(row["id"], 500, ansi=True)
                caption = lines[0] if lines else ""
                content = trim_chrome(row.get("agent"), lines[2:])
                body = [parse_sgr(line) for line in content]
            except Error as error:
                caption, body = str(error), []
            refreshed = now
        text = plain_lines(body)
        visible = max(1, height - 5)
        offset = max(0, min(offset, max(0, len(body) - visible)))
        start = max(0, len(body) - visible - offset)
        block = body[start:start + visible]
        screen.erase()
        put(screen, 0, 0, f"Read-only output: {row['name']}", style.heading)
        hint = ("Arrows/hjkl select | Home/End line | g/G oldest/newest | y copy | Esc cancel"
                if anchor is not None else
                "Up/Down/PgUp/PgDn scroll | Home oldest | End live | Drag or v select | Esc back")
        put(screen, 1, 0, hint, style.help)
        put(screen, 2, 0, caption, style.muted)
        top = 3 + max(0, visible - len(block))
        for index, runs in enumerate(block):
            y, line = top + index, start + index
            put_runs(screen, y, runs, renderer)
            if anchor is not None:
                begin, end = sorted((anchor, cursor))
                if begin[0] <= line <= end[0]:
                    left = begin[1] if line == begin[0] else 0
                    right = end[1] + 1 if line == end[0] else len(text[line])
                    fragment = text[line][left:right]
                    put(screen, y, text_cells(text[line][:left]), fragment or " ",
                        style.selected | curses.A_REVERSE)
        footer = message or ("Selection frozen - y copies; Esc returns to live output"
                             if anchor is not None else
                             "Live tail" if not offset else f"{offset} lines above live tail")
        put(screen, height - 1, 0, footer, style.warning if alert else style.muted)
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", "\x03"):
            if anchor is not None:
                anchor, cursor, dragging, message = None, None, False, ""
                refreshed = 0
                continue
            return
        if key == curses.KEY_MOUSE:
            try:
                _, x, y, _, buttons = curses.getmouse()
            except curses.error:
                continue
            if buttons & curses.BUTTON4_PRESSED:
                offset += 3
                continue
            if buttons & getattr(curses, "BUTTON5_PRESSED", 0):
                offset -= 3
                continue
            if top <= y < top + len(block):
                line = start + y - top
                position = (line, column_index(text[line], x))
                if buttons & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                    anchor = cursor = position
                    dragging, message = bool(buttons & curses.BUTTON1_PRESSED), ""
                elif anchor is not None and dragging:
                    cursor = position
            if buttons & curses.BUTTON1_RELEASED:
                dragging = False
            continue
        if key == "v":
            if anchor is None and text:
                line = next((i for i in range(start, min(len(text), start + visible)) if text[i]), start)
                anchor = cursor = (line, 0)
            else:
                anchor, cursor, dragging = None, None, False
                refreshed = 0
            message = ""
            continue
        if anchor is not None:
            line, column = cursor
            if key in ("y", "\x19"):
                try:
                    message, alert = copy_text(selected_text(text, anchor, cursor), tmux), False
                except (Error, OSError) as error:
                    message, alert = str(error), True
                continue
            if key in (curses.KEY_UP, "k"):
                line -= 1
            elif key in (curses.KEY_DOWN, "j"):
                line += 1
            elif key in (curses.KEY_LEFT, "h"):
                column -= 1
            elif key in (curses.KEY_RIGHT, "l"):
                column += 1
            elif key == curses.KEY_PPAGE:
                line -= visible
            elif key == curses.KEY_NPAGE:
                line += visible
            elif key == curses.KEY_HOME:
                column = 0
            elif key == curses.KEY_END:
                column = max(0, len(text[line]) - 1)
            elif key == "g":
                line, column = 0, 0
            elif key == "G":
                line, column = len(text) - 1, max(0, len(text[-1]) - 1)
            line = max(0, min(line, len(text) - 1))
            column = max(0, min(column, max(0, len(text[line]) - 1)))
            cursor, message = (line, column), ""
            if line < start:
                offset = len(body) - visible - line
            elif line >= start + visible:
                offset = len(body) - line - 1
            continue
        if key == curses.KEY_UP:
            offset += 1
        elif key == curses.KEY_DOWN:
            offset -= 1
        elif key == curses.KEY_PPAGE:
            offset += visible
        elif key == curses.KEY_NPAGE:
            offset -= visible
        elif key == curses.KEY_HOME:
            offset = max(0, len(body) - visible)
        elif key == curses.KEY_END:
            offset = 0
        elif key == "\x0c":
            refreshed = 0


HELP_SECTIONS = (
    ("Search and move", (
        ("Type", "filter by session name, agent, command, or directory"),
        ("Enter on no match", "create a new session named by what you typed"),
        ("Up/Down, Tab", "move the selection"),
        ("Right/Left, Ctrl-T", "switch between Live and History"),
        ("PgUp/PgDn, Home/End", "jump through the list"),
        ("Ctrl-U", "clear the search"),
        ("Ctrl-L", "refresh sessions and preview now"),
    )),
    ("Sessions", (
        ("Enter", "attach, or (in History) restore a closed session"),
        ("Ctrl-N", "new session: agent, directory, name (blank name = auto)"),
        ("Ctrl-R", "rename the selected session"),
        ("Ctrl-D, Delete", "delete a live session or forget a history entry (press y)"),
        ("Ctrl-W", "add a shell window in the session's directory"),
    )),
    ("View", (
        ("Ctrl-P", "show or hide the pane preview"),
        ("Ctrl-O", "read-only output viewer; drag or v selects text, y copies"),
        ("Ctrl-G", "usage limits and session cost, read locally"),
        ("F1", "this help"),
        ("Esc", "leave the picker or panel"),
    )),
    ("Inside tmux", (
        ("Ctrl-b Ctrl-s", "open the picker as a popup, staying inside tmux"),
        ("Alt-s", "detach and open the picker in this terminal"),
        ("Alt-x", "kill the current session after a y/n confirmation"),
        ("", "Alt-s is not a popup: choosing a session reattaches you, and Esc"),
        ("", "returns to the shell. Alt-x stops every program in the session."),
    )),
    ("Command line", (
        ("agmux --help", "every command and option"),
        ("agmux list --json", "one JSON array of sessions"),
        ("agmux info NAME --json", "one session's details as JSON"),
        ("agmux usage --json", "local limits and cost as JSON"),
        ("agmux save", "save all live workspaces now"),
        ("agmux saved", "list workspaces available for recovery"),
        ("agmux restore --all", "restore saved workspaces, detached"),
        ("agmux forget NAME", "remove a saved definition; keep any live session"),
        ("agmux doctor", "tools, usage sources, and tmux integration"),
    )),
    ("What you are seeing", (
        ("", "Sessions whose agent needs input are flagged and sorted first."),
        ("", "Finished sessions get a check mark and a notification."),
        ("", "Closed and post-reboot sessions live in the History tab (Right arrow),"),
        ("", "not on the main page. Enter there restores windows and panes."),
        ("", "Supported agents reopen their saved conversation when its ID is known."),
        ("", "cpu X% and mem Y are this machine's CPU and RAM for the session's"),
        ("", "processes, not token, context-window, or rate-limit usage."),
        ("", "Deleting ends the session and its programs; files on disk are"),
        ("", "untouched. Previews and the viewer keep the pane's own colors."),
        ("", "Usage and previews are local, with no telemetry. Discord alerts are optional."),
    )),
)


def build_help_lines(width):
    """Lay out help as styled segments, wrapping cleanly at any width."""
    key_width = max(len(key) for _, entries in HELP_SECTIONS
                    for key, _ in entries if key)
    column = min(24, key_width + 2)
    stacked = width < column + 34
    lines = []
    for title, entries in HELP_SECTIONS:
        if lines:
            lines.append([("blank", "")])
        lines.append([("section", title)])
        for key, description in entries:
            if not key:
                for chunk in textwrap.wrap(description, max(16, width - 4)) or [""]:
                    lines.append([("note", "  " + chunk)])
                continue
            if stacked:
                lines.append([("key", "  " + key)])
                for chunk in textwrap.wrap(description, max(12, width - 6)) or [""]:
                    lines.append([("note", "      " + chunk)])
                continue
            lead = "  " + key.ljust(column)
            chunks = textwrap.wrap(description, max(12, width - column - 2)) or [""]
            lines.append([("key", lead), ("desc", chunks[0])])
            for chunk in chunks[1:]:
                lines.append([("note", "  " + " " * column + chunk)])
    return lines


def show_help(screen, style=None):
    style = style or Style()
    attributes = {"section": style.heading, "key": style.name,
                  "desc": style.default, "note": style.help, "blank": 0}
    lines, lines_width, offset = [], 0, 0
    screen.timeout(200)
    while True:
        height, width = screen.getmaxyx()
        if width != lines_width:
            lines = build_help_lines(max(20, width))
            lines_width = width
        visible = max(1, height - 3)
        offset = max(0, min(offset, max(0, len(lines) - visible)))
        screen.erase()
        put(screen, 0, 0, f"{APP} {VERSION}", style.heading)
        put_right(screen, 0, "keyboard help", style.muted)
        put(screen, 1, 0, GLYPH["sep"] * max(1, width - 1), style.muted)
        for index, segments in enumerate(lines[offset:offset + visible]):
            x = 0
            for role, text in segments:
                put(screen, 2 + index, x, text, attributes[role])
                x += len(text)
        if len(lines) <= visible:
            footer = "Esc, Enter, or F1 returns to the picker"
        else:
            last = min(offset + visible, len(lines))
            footer = (f"lines {offset + 1}-{last} of {len(lines)}   "
                      "Up/Down, PgUp/PgDn, Home/End scroll | Esc/Enter/F1 return")
        put(screen, height - 1, 0, footer, style.muted)
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", "\x03", "\n", "\r", curses.KEY_ENTER, curses.KEY_F1):
            return
        if key == curses.KEY_UP:
            offset -= 1
        elif key == curses.KEY_DOWN:
            offset += 1
        elif key == curses.KEY_PPAGE:
            offset -= visible
        elif key == curses.KEY_NPAGE:
            offset += visible
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = len(lines)


def show_usage_panel(screen, style=None):
    style = style or Style()
    width = max(10, min(28, screen.getmaxyx()[1] - 24))
    screen.timeout(-1)
    screen.erase()
    put(screen, 0, 0, f"{APP} usage limits", style.heading)
    put(screen, 1, 0, "Read locally from Codex logs and OpenCode's database.",
        style.help)
    for index, (text, role) in enumerate(usage_report(width)):
        put(screen, 3 + index, 0, text, getattr(style, role, 0))
    put(screen, screen.getmaxyx()[0] - 1, 0, "Any key returns to the picker", style.muted)
    screen.refresh()
    while True:
        try:
            if screen.get_wch():
                break
        except curses.error:
            continue
    screen.timeout(200)


def confirm_delete_answer(name, answer):
    """The deliberate confirmation: the typed text must exactly match the name."""
    return answer.rstrip("\r\n") == name


def describe_lines(info, style=None, color=False):
    """Human-readable deletion summary, shared by CLI and the picker."""
    return [f"Session: {clean(info['name'])}  ({info['id']})",
            f"Windows: {info['windows']}   Panes: {info['pane_count']}   "
            f"Attached clients: {info['client_count']}"] + [
        f"  {'*' if pane['active'] else ' '} window {pane['window']}: "
        f"{clean(pane['command'])}  {short_path(pane['path'])}"
        for pane in info["panes"]]


def confirm_delete(screen, tmux, info, style=None):
    """Red confirmation screen; a single ``y`` keypress confirms.

    Returns "deleted", "cancelled", or "changed". On "changed" nothing was
    deleted and the caller should ask the user to start confirmation again.
    """
    style = style or Style()
    message, result = "", "cancelled"
    screen.timeout(-1)
    while True:
        height, width = screen.getmaxyx()
        screen.erase()
        put(screen, 0, 0, "Delete session - this cannot be undone", style.warning)
        details = describe_lines(info)
        room = max(1, height - 10)
        for index, line in enumerate(details[:room]):
            attr = style.heading if index == 0 else (style.muted if index > 1 else 0)
            put(screen, 2 + index, 0, line, attr)
        if len(details) > room:
            put(screen, 2 + room, 0, f"... and {len(details) - room} more pane(s)", style.muted)
        put(screen, height - 4, 0,
            "Ending the live session stops its running programs.", style.warning)
        put(screen, height - 3, 0, "It does not delete project files on disk.", style.muted)
        if message:
            put(screen, height - 5, 0, message, style.warning)
        else:
            put(screen, height - 5, 0, "Press y to delete, or any other key to cancel.",
                style.help)
        put(screen, height - 2, 0, "Delete this session? [y/N]", style.warning)
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key not in ("y", "Y"):
            result = "cancelled"
            break
        try:
            tmux.delete(info["id"], expected=info)
            result = "deleted"
        except Error as error:
            message = str(error)
            result = "changed"
        break
    screen.timeout(200)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    return result


_STATE_WORD = {"blocked": "needs input", "running": "working", "finished": "done",
               "idle": "idle", "shell": "shell", "saved": "closed"}


def fit(text, width):
    """Pad or ellipsize ``text`` to exactly ``width`` cells."""
    text = clean(text)
    if len(text) > width:
        mark = GLYPH["ellipsis"]
        return text[:max(0, width - len(mark))] + mark
    return text.ljust(width)


def draw_row(screen, y, row, selected, style, name_width=18):
    """One aligned line: marker, name, agent, state, resources, directory, age."""
    _, width = screen.getmaxyx()
    status = row_status(row)
    glyph = {"finished": GLYPH["finished"], "running": GLYPH["running"],
             "blocked": GLYPH["blocked"], "idle": GLYPH["idle"],
             "saved": "\u25cb" if GLYPH["dot"] != "*" else "o"}.get(status, GLYPH["shell"])
    glyph_role = {"finished": "finished", "running": "attached",
                  "blocked": "warning", "idle": "muted", "saved": "muted"}.get(status, "muted")
    agent = AGENTS.get(row.get("agent") or "")
    state = _STATE_WORD.get(status, status)
    state_role = {"blocked": "warning", "running": "attached",
                  "finished": "finished"}.get(status, "muted")
    segments = [(" " + glyph + " ", glyph_role),
                (fit(row["name"], name_width) + "  ", "name"),
                (fit(agent.label if agent and not agent.is_shell else "shell", 9) + " ",
                 agent.accent if agent and not agent.is_shell else "muted"),
                (fit(state, 11) + " ", state_role)]
    if row.get("saved"):
        segments.append((fit(f"{row['windows']} win", 7), "muted"))
    else:
        if row["attached"]:
            label = "attached" if row["attached"] == 1 else f"{row['attached']} clients"
            segments.append((fit(label, 9) + " ", "attached"))
        else:
            segments.append((" " * 10, "muted"))
        resource = resource_label(row.get("cpu"), row.get("rss"))
        if resource:
            segments.append((resource + "  ", "secondary"))
    if row.get("cwd"):
        segments.append((short_path(row["cwd"]), "muted"))
    age = humanize_age(row.get("activity"))
    if age:
        age = age + (" ago" if row.get("saved") else "")
        text_len = sum(len(clean(t)) for t, _ in segments)
        room = width - 1 - text_len - len(age) - 2
        if room >= 0:
            segments.append((" " * (room + 2), "muted"))
            segments.append((age, "muted"))
    if selected:
        line = "".join(clean(text) for text, _ in segments)
        put(screen, y, 0, line.ljust(width - 1), style.selected)
        return
    x = 0
    for text, role in segments:
        text = clean(text)
        put(screen, y, x, text, getattr(style, role, 0))
        x += len(text)


def choose(screen, title, options, style, hint="Type to filter | Up/Down select | Enter confirm | Esc cancel",
           paths=False):
    """A small searchable list chooser. Returns the selected value or None.

    With ``paths`` a query that looks like a directory (starts with ``/``,
    ``~`` or ``.``) becomes the first option, so you can type a path directly.
    """
    screen.keypad(True)
    screen.timeout(200)
    query, selection = "", 0
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    while True:
        matches = [o for o in options
                   if fuzzy(query, o["label"] + " " + o.get("note", ""))]
        if paths and query:
            term = query.casefold()
            matches.sort(key=lambda o: term not in o["label"].casefold())
        if paths and query[:1] in ("/", "~", "."):
            typed = os.path.abspath(os.path.expanduser(query))
            if os.path.isdir(typed):
                matches.insert(0, dict(label=query, note="use this directory", value=typed))
            else:
                matches = [dict(label=query, note="Tab completes - not a directory yet",
                                value=None, enabled=False)] + matches
        selection = max(0, min(selection, len(matches) - 1))
        height, width = screen.getmaxyx()
        screen.erase()
        put(screen, 0, 0, title, style.heading)
        put(screen, 1, 0, hint, style.help)
        put(screen, 2, 0, "\u203a " if GLYPH["dot"] != "*" else "> ", style.heading)
        if query:
            put(screen, 2, 2, query, style.default)
        else:
            put(screen, 2, 2, "type to filter" + (", or paste a path" if paths else ""),
                style.muted)
        visible = max(1, height - 5)
        start = max(0, selection - visible + 1)
        for index, option in enumerate(matches[start:start + visible], start):
            text = (GLYPH["idle"] + " " if index == selection else "  ") + option["label"]
            if option.get("note"):
                text += "  " + option["note"]
            attr = style.selected if index == selection else (
                style.muted if not option.get("enabled", True) else style.heading)
            put(screen, 3 + index - start, 0, text, attr)
        if not matches:
            put(screen, 3, 0, "No matches", style.muted)
        put(screen, height - 1, 0, f"{len(matches)} option(s)", style.muted)
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", "\x03"):
            return None
        if key in ("\n", "\r", curses.KEY_ENTER) and matches:
            if matches[selection].get("enabled", True):
                return matches[selection].get("value", matches[selection]["label"])
            continue
        if key == "\t" and paths and query[:1] in ("/", "~", "."):
            query = complete_path(query)
        elif key in (curses.KEY_DOWN, "\t"):
            selection = (selection + 1) % max(1, len(matches))
        elif key in (curses.KEY_UP, curses.KEY_BTAB):
            selection = (selection - 1) % max(1, len(matches))
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            query = query[:-1]
        elif key == "\x15":
            query, selection = "", 0
        elif isinstance(key, str) and key.isprintable():
            query += key


def _read_project_history(path, tmux):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    agent = data.get("agent") if isinstance(data.get("agent"), str) else ""
    if isinstance(data.get("projects"), list):
        # The file stores ordered pairs. Insertion order breaks frequency ties.
        uses = {}
        for item in data["projects"]:
            if (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
                    and os.path.isabs(item[0]) and "\0" not in item[0]
                    and type(item[1]) is int and item[1] > 0):
                uses[item[0]] = item[1]
        return agent, uses, False

    # Old releases kept eight recent paths but no counts. Each saved workspace
    # supplies one observed use; a recent path may already be represented there.
    counts = {}
    if recovery_enabled():
        try:
            for record in tmux.workspace_store().read().values():
                directory = record["cwd"]
                if os.path.isabs(directory):
                    directory = os.path.realpath(directory)
                    counts[directory] = counts.get(directory, 0) + 1
        except (Error, OSError):
            pass
    uses = {}
    recent = data.get("dirs")
    if isinstance(recent, list):
        for directory in recent:
            if isinstance(directory, str) and os.path.isabs(directory) and "\0" not in directory:
                directory = os.path.realpath(directory)
                if directory not in uses:
                    uses[directory] = counts.pop(directory, 1)
    # Saved checkpoints carry no reliable last-selection time.
    for directory in sorted(counts, key=lambda d: (Path(d).name.casefold(), d)):
        uses[directory] = counts[directory]
    return agent, uses, True


def project_history(tmux, cwd=None, selected_agent=None):
    """Return last agent and use counts in most-recent-first order."""
    path = state_directory() / "recent.json"
    agent, uses, migrate = _read_project_history(path, tmux)
    if not migrate and cwd is None:
        return agent, uses
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            agent, uses, migrate = _read_project_history(path, tmux)
            if cwd is not None:
                agent = selected_agent or agent
                count = uses.pop(cwd, 0)
                uses = {cwd: count + 1, **uses}
            if migrate or cwd is not None:
                fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".recent-")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump({"agent": agent, "projects": list(uses.items())}, handle)
                    os.replace(tmp, path)
                finally:
                    Path(tmp).unlink(missing_ok=True)
    except OSError:
        pass  # Picker preferences are optional, including on a read-only filesystem.
    return agent, uses


def new_session_flow(screen, tmux, style, notify=True, name=None):
    """Ctrl-N wizard: agent, then directory, then an optional name.

    The last agent and frequently selected directories float to the top, a typed path is
    accepted directly, and a blank name means "use the suggestion".
    """
    last_agent, uses = project_history(tmux)
    options = []
    for agent in AGENTS.values():
        if agent.is_shell:
            options.append(dict(label="Shell", note="a plain tmux session",
                                value="shell", enabled=True))
        elif shutil.which(agent.binary):
            options.append(dict(label=agent.label, note=f"start {agent.binary}",
                                value=agent.key, enabled=True))
    options.sort(key=lambda o: o["value"] != last_agent)
    if options and options[0]["value"] == last_agent:
        options[0]["note"] += "  (last used)"
    agent = choose(screen, "New session (1/3): what should it run?", options, style)
    if agent is None:
        return None

    directories, seen = [], set()

    def add(label, note, value):
        path = Path(value)
        if path.is_dir():
            canonical = str(path.resolve())
            if canonical not in seen:
                seen.add(canonical)
                directories.append(dict(label=label, note=note, value=canonical))

    for directory in sorted(uses, key=uses.get, reverse=True):
        count = uses[directory]
        label = "~" if directory == str(Path.home()) else Path(directory).name or directory
        add(label, f"{count} session{'s' if count != 1 else ''}  {short_path(directory)}", directory)
    add(str(Path.cwd()), "current directory", str(Path.cwd()))
    try:
        for repo in repos():
            add(repo["name"], short_path(repo["id"]), repo["id"])
    except Error:
        pass
    add("~", "home directory", str(Path.home()))
    cwd = choose(screen, f"New {AGENTS[agent].label} session (2/3): which directory?",
                 directories, style, paths=True,
                 hint="Type to filter, or a path (Tab completes) | Enter confirm | Esc cancel")
    if cwd is None:
        return None

    suggestion = tmux.suggested_name(cwd, agent)
    auto_name = False
    if name is None:
        name = prompt(screen, "Name (3/3)", "", style, placeholder=suggestion + "  (Enter = use this)")
        if name is None:
            return None
        auto_name = not name.strip()
        name = name.strip() or suggestion
    sid = tmux.create(name, cwd, agent=agent, notify=notify, auto_name=auto_name, remember_agent=True)
    return dict(id=sid, name=name)


def picker(screen, tmux, show_preview=True, style=None, notify=True, resources=True,
           origin_client=None):
    style = style or setup_styles()
    screen.keypad(True)
    screen.timeout(200)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    query, selection, selected_id = "", 0, None
    view = "live"
    live_rows, saved_list = [], []
    rows, refreshed, message, message_alert = [], 0.0, "", False
    preview_key, preview_at, follow_key = None, 0.0, None
    preview_caption, preview_body = "", []
    renderer = AnsiRenderer()
    while True:
        now = time.monotonic()
        if now - refreshed > 2:
            try:
                live_rows = tmux.session_details(detect=True, resources=resources)
            except Error as error:
                live_rows, message, message_alert = [], str(error), True
            try:
                saved_list = saved_rows(tmux)
                if tmux.recovery_error:
                    message, message_alert = tmux.recovery_error, True
            except (Error, OSError) as error:
                saved_list, message, message_alert = [], str(error), True
            refreshed = now
        rows = live_rows if view == "live" else saved_list
        ordered = sorted(rows, key=lambda r: (state_priority(r), -r["activity"], r["name"]))
        matches = [row for row in ordered if fuzzy(query, searchable(row))]
        # Preserve selection across refreshes and narrowing the filter.
        if selected_id:
            selection = next((i for i, row in enumerate(matches) if row["id"] == selected_id),
                             selection)
        selection = max(0, min(selection, len(matches) - 1))
        selected = matches[selection] if matches else None
        selected_id = selected["id"] if selected else None
        height, width = screen.getmaxyx()
        if height < 8 or width < 30:
            screen.erase()
            put(screen, 0, 0, "Enlarge terminal; Esc exits")
            screen.refresh()
            try:
                if screen.get_wch() in ("\x1b", "\x03"):
                    return None
            except curses.error:
                pass
            continue
        screen.erase()
        statuses = [row_status(row) for row in live_rows]
        running = statuses.count("running")
        finished = statuses.count("finished")
        blocked = statuses.count("blocked")
        mid = GLYPH["mid"]
        title = f"{APP} {VERSION}"
        bits = [f"{len(live_rows)} live"]
        if running:
            bits.append(f"{running} working")
        if blocked:
            bits.append(f"{blocked} need input")
        if finished:
            bits.append(f"{finished} done")
        summary = mid.join(bits)
        if len(title) + len(summary) + 2 > width:
            summary = f"{len(live_rows)} live" + (f"{mid}{blocked}!" if blocked else "")
        put(screen, 0, 0, title, style.heading)
        put_right(screen, 0, summary, style.warning if blocked else style.muted)
        # Tabs: the main page is live sessions only; history is one keypress away.
        live_tab = f" Live {len(live_rows)} "
        hist_tab = f" History {len(saved_list)} "
        put(screen, 1, 0, live_tab, style.selected if view == "live" else style.muted)
        put(screen, 1, len(live_tab) + 1, hist_tab,
            style.selected if view == "history" else style.muted)
        usage = "   ".join(part for part in (usage_inline(), opencode_inline()) if part)
        tabs_end = len(live_tab) + len(hist_tab) + 3
        if usage and width - tabs_end - 2 >= len(usage):
            put_right(screen, 1, usage, style.muted)
        put(screen, 2, 0, "\u203a " if GLYPH["dot"] != "*" else "> ", style.heading)
        if query:
            put(screen, 2, 2, query, style.default)
        else:
            hint = ("type to filter, or a new name then Enter to create" if view == "live"
                    else "type to filter closed sessions")
            put(screen, 2, 2, hint, style.muted)
        list_top = 3
        list_height = max(1, (height - list_top - 2) // 2) if show_preview \
            else max(1, height - list_top - 2)
        start = max(0, selection - list_height + 1)
        name_width = max(10, min(24, max((len(r["name"]) for r in matches), default=10)))
        for index, row in enumerate(matches[start:start + list_height], start):
            draw_row(screen, list_top + index - start, row, index == selection, style,
                     name_width)
        if not matches:
            other = len(saved_list) if view == "live" else len(live_rows)
            if view == "live" and query:
                lines = [(f"No live session matches '{query}'.", style.muted),
                         (f"Enter creates a new session named '{query}'.", style.heading)]
            elif view == "live":
                lines = [("No live sessions.", style.muted),
                         ("Ctrl-N starts one.", style.heading)]
            elif query:
                lines = [(f"Nothing in history matches '{query}'.", style.muted)]
            else:
                lines = [("History is empty. Closed sessions you can restore show up here.",
                          style.muted)]
            if other and view == "live":
                lines.append((f"{other} closed session(s) in History (press Right).", style.help))
            for offset, (text, attr) in enumerate(lines):
                put(screen, list_top + offset, 2, text, attr)
        if show_preview:
            # Give the preview every row the list does not need, so it grows
            # upward when there are few sessions and never shrinks below the
            # current half-and-half minimum.
            used = max(1, min(len(matches), list_height))
            preview_y = list_top + used + 1
            put(screen, preview_y - 1, 0, GLYPH["sep"] * (width - 1), style.muted)
            if selected:
                if selected_id != preview_key or now - preview_at > 2:
                    followed = False
                    try:
                        if selected_id != follow_key:
                            if not selected.get("saved"):
                                followed = tmux.follow_preview(selected_id, origin_client)
                            follow_key = selected_id
                        if selected.get("saved"):
                            if selected.get("resumes_chat"):
                                detail = f"Resumes this {AGENTS[selected['agent']].label} conversation."
                            elif selected["agent"] in ("codex", "claude"):
                                detail = "No conversation ID was saved. Choose the chat in the agent's history."
                            else:
                                detail = "Restores shell panes; start the agent when ready."
                            lines = ["Saved workspace | " + short_path(selected["cwd"]), "",
                                     "Enter restores windows, panes, layouts, and directories.", detail,
                                     "Ctrl-D forgets this saved workspace."]
                        else:
                            lines = tmux.preview(selected_id, max(1, height - preview_y - 6),
                                                 ansi=True)
                        preview_caption = lines[0] if lines else ""
                        content = trim_chrome(selected.get("agent"), lines[2:])
                        preview_body = [parse_sgr(line) for line in content]
                    except Error as error:
                        preview_caption, preview_body = str(error), []
                    # Repaint promptly after the agent handles the navigation event.
                    preview_key, preview_at = selected_id, now - 2 if followed else now
                put(screen, preview_y, 0, preview_caption, style.muted)
                offset = 1
                if row_status(selected) == "blocked" and selected.get("prompt"):
                    put(screen, preview_y + offset, 0,
                        "! needs input: " + selected["prompt"], style.warning)
                    offset += 1
                room = max(0, height - preview_y - offset - 1)
                visible = preview_body[-room:] if room else []
                # Hug the bottom of the preview area, matching a real terminal:
                # short content sits at the bottom instead of floating at the top.
                top = preview_y + offset + max(0, room - len(visible))
                for index, runs in enumerate(visible):
                    put_runs(screen, top + index, runs, renderer)
        if message:
            footer = message
        elif view == "history":
            footer = (f"{len(matches)} of {len(saved_list)} closed   Enter restore {mid}"
                      f" Ctrl-D forget {mid} Left back to live {mid} F1 help").replace("  ", " ")
        elif not matches and query:
            footer = f"Enter create '{query}' {mid} Ctrl-U clear {mid} Right history {mid} F1 help"
        else:
            footer = (f"{len(matches)} of {len(live_rows)}   Enter attach {mid} Ctrl-N new {mid}"
                      f" Ctrl-R rename {mid} Ctrl-D delete {mid} Right history {mid} F1 help")
        footer = footer.replace("  ", " ")
        put(screen, height - 1, 0, footer, style.warning if message_alert else style.muted)
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", "\x03"):
            return None
        if key in ("\n", "\r", curses.KEY_ENTER) and not selected and view == "live" \
                and query.strip():
            try:
                created = new_session_flow(screen, tmux, style, notify=notify,
                                           name=query.strip())
                if created:
                    return created
            except Error as error:
                message, message_alert = str(error), True
            refreshed = 0
            continue
        if key in ("\n", "\r", curses.KEY_ENTER) and selected:
            if selected.get("saved"):
                try:
                    sid = tmux.restore(selected["name"], notify=notify)
                    return tmux.resolve(sid)
                except (Error, OSError) as error:
                    message, message_alert = str(error), True
                    refreshed = 0
                    continue
            return selected
        if message and not (key == -1):
            message, message_alert = "", False
        if key in (curses.KEY_RIGHT, curses.KEY_LEFT, "\x14", curses.KEY_F2):
            target = {curses.KEY_RIGHT: "history", curses.KEY_LEFT: "live"}.get(
                key, "history" if view == "live" else "live")
            if target != view:
                view, selection, selected_id, query = target, 0, None, ""
                preview_at, follow_key = 0, None
                rows = live_rows if view == "live" else saved_list
        elif key in (curses.KEY_DOWN, "\t"):
            selection = (selection + 1) % max(1, len(matches))
            selected_id = None
        elif key in (curses.KEY_UP, curses.KEY_BTAB):
            selection = (selection - 1) % max(1, len(matches))
            selected_id = None
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            if key == curses.KEY_PPAGE:
                selection = max(0, selection - list_height)
            elif key == curses.KEY_NPAGE:
                selection = min(max(0, len(matches) - 1), selection + list_height)
            elif key == curses.KEY_HOME:
                selection = 0
            else:
                selection = max(0, len(matches) - 1)
            selected_id = None
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            query = query[:-1]
        elif key == "\x15":
            query, selection, selected_id = "", 0, None
        elif key == "\x0c":
            refreshed, preview_at, follow_key = 0, 0, None
        elif key == "\x10":
            show_preview = not show_preview
            preview_at, follow_key = 0, None
        elif key == "\x07":
            show_usage_panel(screen, style)
        elif key == "\x0f" and selected and not selected.get("saved"):
            viewer(screen, tmux, selected, style)
            refreshed, preview_at, follow_key = 0, 0, None
        elif key == curses.KEY_F1:
            show_help(screen, style)
        elif key == "\x12" and selected and not selected.get("saved"):
            name = prompt(screen, "Rename", "", style,
                          placeholder=selected["name"] + "  (type a new name)")
            if name is not None and name.strip() and name.strip() != selected["name"]:
                name = name.strip()
                try:
                    tmux.rename(selected_id, name)
                    message, message_alert, refreshed = "Session renamed", False, 0
                except Error as error:
                    message, message_alert = str(error), True
        elif key in ("\x04", curses.KEY_DC) and selected:
            try:
                if selected.get("saved"):
                    if confirm_key(screen, f"Forget saved '{selected['name']}'?", style):
                        tmux.workspace_store().forget(selected["name"])
                        message, message_alert = "Forgot saved workspace", False
                    refreshed, preview_at = 0, 0
                    continue
                info = tmux.describe(selected_id)
                outcome = confirm_delete(screen, tmux, info, style)
                if outcome == "deleted":
                    message, message_alert = f"Deleted session {info['name']}", False
                elif outcome == "changed":
                    message = "Session changed; start the confirmation again."
                    message_alert = True
            except Error as error:
                message, message_alert = str(error), True
            refreshed, preview_at = 0, 0
        elif key == "\x17" and selected and not selected.get("saved"):
            try:
                index = tmux.shell_window(selected_id)
                message = (f"Added shell window {index} to {selected['name']}. "
                           "Select it with Ctrl-b then w")
                message_alert = False
            except Error as error:
                message, message_alert = str(error), True
            refreshed = 0
        elif key == "\x0e":
            try:
                created = new_session_flow(screen, tmux, style, notify=notify)
                if created:
                    return created
            except Error as error:
                message, message_alert = str(error), True
            refreshed = 0
        elif isinstance(key, str) and key.isprintable():
            query += key


def interactive(tmux, client=None, show_preview=True, notify=True, resources=True):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Error(f"The picker requires a terminal. Use {APP} list for text output.")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    client = tmux.client(client)
    selected = curses.wrapper(picker, tmux, show_preview=show_preview, notify=notify,
                              resources=resources, origin_client=client)
    return tmux.connect(selected["id"], client) if selected else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def session_record(row):
    """A stable, machine-readable view of one session."""
    agent = AGENTS.get(row.get("agent") or "")
    return dict(id=row["id"], name=row["name"], agent=row.get("agent") or "",
                agent_label=agent.label if agent and not agent.is_shell else "",
                status=row_status(row), state=row.get("state") or "",
                attached=bool(row["attached"]), clients=int(row.get("attached") or 0),
                windows=row["windows"], unread=int(row.get("unread") or 0),
                process=row.get("process") or "", cwd=row.get("cwd") or "",
                cpu=round(float(row.get("cpu") or 0.0), 1), rss=row.get("rss") or 0,
                prompt=row.get("prompt") or "", activity=row.get("activity"),
                agent_pane=row.get("agent_pane") or "")


def info_record(tmux, target, resources=True):
    """Detailed, machine-readable view of one resolved session."""
    resolved = tmux.resolve(target)
    details = next((row for row in tmux.session_details(detect=True, resources=resources)
                    if row["id"] == resolved["id"]), None)
    if details is None:
        raise Error("That session no longer exists.")
    info = tmux.describe(resolved["id"])
    record = session_record(details)
    record["panes"] = info["panes"]
    record["pane_count"] = info["pane_count"]
    record["client_ttys"] = info["clients"]
    return record


def info_lines(record, enabled=False):
    """Human-readable detail view used by ``agmux info``."""
    label = record.get("agent_label") or "Shell"
    lines = [paint(f"{clean(record['name'])}  ({record['id']})", "heading", enabled)]
    lines.append(f"  agent: {label}   status: {record['status']}   "
                 f"clients: {record['clients']}   windows: {record['windows']}")
    lines.append(f"  process: {clean(record.get('process') or '-')}   "
                 f"directory: {paint(short_path(record.get('cwd') or ''), 'muted', enabled)}")
    resource = resource_label(record.get("cpu"), record.get("rss"))
    if resource:
        lines.append(f"  {resource}")
    if record.get("prompt"):
        lines.append(paint(f"  needs input: {clean(record['prompt'])}", "warning", enabled))
    for pane in record.get("panes", []):
        marker = "*" if pane["active"] else " "
        lines.append(f"  {marker} window {pane['window']}: "
                     f"{clean(pane['command'])}  {short_path(pane['path'])}")
    if record.get("client_ttys"):
        lines.append("  clients: " + ", ".join(clean(tty) for tty in record["client_ttys"]))
    return lines


def list_line(row, enabled):
    """One human-readable session line (with optional color), as a string."""
    agent = AGENTS.get(row.get("agent") or "")
    status = row_status(row)
    glyph = {"finished": GLYPH["finished"], "running": GLYPH["running"],
             "blocked": GLYPH["blocked"], "idle": GLYPH["idle"]}.get(status, GLYPH["shell"])
    role = {"finished": "finished", "running": "attached",
            "blocked": "warning", "idle": "muted"}.get(status, "muted")
    name = f"{clean(row['name']):<28}"
    if row["attached"] == 1:
        state_text = "attached"
    elif row["attached"]:
        state_text = f"{row['attached']} clients"
    else:
        state_text = "detached"
    state = f"{state_text:<8}"
    segments = [(f"{glyph} ", role), (name, "heading")]
    if agent and not agent.is_shell:
        segments.append((f" {agent.label:<9}", agent.accent))
    else:
        segments.append((f" {'-':<9}", "muted"))
    segments.append((f"{state}  ", "attached" if row["attached"] else "muted"))
    segments.append((f"{clean(row.get('process', '')):<10} ", "default"))
    segments.append((short_path(row.get("cwd", "")), "muted"))
    resource = resource_label(row.get("cpu"), row.get("rss"))
    if resource:
        segments.append(("  " + resource, "secondary"))
    if status == "blocked" and row.get("prompt"):
        segments.append(("  needs input: " + row["prompt"][:100], "warning"))
    elif status in ("finished", "exited"):
        segments.append(("  " + status, "finished" if status == "finished" else "muted"))
    return paint_segments(segments, enabled)


def list_lines(tmux, enabled, resources=True):
    for row in tmux.session_details(detect=True, resources=resources):
        print(list_line(row, enabled))


def _tmux_command_available(tmux, name):
    try:
        tmux.run("list-commands", name, optional=True)
        return True
    except Error:
        return False


def doctor(tmux):
    print(f"{APP} {VERSION}")
    version = tmux.run("-V")
    print(f"tmux: {version}")
    print(f"Helper: {SELF}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    socket = _env("AGMUX_TMUX_SOCKET", "CX_TMUX_SOCKET")
    print(f"tmux socket: {socket or 'current/default'}")
    print(f"Inside tmux: {'yes' if os.environ.get('TMUX') else 'no'}")
    popup = _tmux_command_available(tmux, "display-popup")
    print(f"Popup integration: {'supported' if popup else 'not supported by this tmux'}")
    print(f"Repositories: {', '.join(str(path) for path in repos_roots())}")
    config = config_path()
    print(f"Config file: {config} "
          f"({'present' if config.is_file() else 'not present'})")
    if CONFIG_APPLIED:
        print(f"Config applied: {', '.join(sorted(CONFIG_APPLIED))}")
    print(f"Workspace recovery: {'on' if recovery_enabled() else 'off'}")
    print(f"Recovery file: {tmux.workspace_store().path}")
    print(f"Save interval: {save_interval():g}s (0 disables background saving)")
    try:
        print(f"Saved workspaces available: {len(saved_rows(tmux))}")
    except (Error, OSError) as error:
        print(f"Recovery error: {clean(error)}")
    print(f"Default agent: {DEFAULT_AGENT}")
    for agent in AGENTS.values():
        if agent.is_shell:
            continue
        found = shutil.which(agent.binary)
        print(f"  {agent.label:<9} {found or 'missing'}")
    notify = 'on' if notify_default() else 'off'
    if os.environ.get("AGMUX_NOTIFY_CMD"):
        try:
            route = notification_argv(os.environ["AGMUX_NOTIFY_CMD"], "title", "body", "session", 0)
            notify += " (Discord)" if route[:2] == [APP, "discord"] else " (custom command)"
        except (ValueError, KeyError, IndexError):
            notify += " (invalid notify_cmd quoting or placeholder)"
    elif not shutil.which("notify-send"):
        notify += " (notify-send not found)"
    print(f"Notifications: {notify}")
    print(f"Watch interval: {watch_interval()}s")
    print(f"Preview auto-follow: {'on' if preview_follow_enabled() else 'off'} (supported chats)")
    print(f"Resources: {'on' if resources_enabled() else 'off'}"
          f"{'' if _process_table() else ' (ps unavailable)'}")
    print(f"ASCII glyphs: {'on' if os.environ.get('AGMUX_ASCII') else 'off'}")
    limits = codex_usage(0)
    print(f"Codex usage: {'available' if limits else 'none found'}"
          f" ({CODEX_HOME / 'sessions'})")
    opencode = opencode_usage(0)
    if opencode:
        print(f"OpenCode usage: {opencode['source']} ({opencode['table']})")
    elif OPENCODE_DB.is_file():
        print(f"OpenCode usage: {OPENCODE_DB} (no readable session table)")
    else:
        print(f"OpenCode usage: no database at {OPENCODE_DB}")
    sessions = tmux.session_details(detect=True)
    statuses = [row_status(row) for row in sessions]
    print(f"Sessions: {len(sessions)} "
          f"({statuses.count('blocked')} need input, "
          f"{statuses.count('finished')} finished)")


class WatchedCompletion:
    """Follow one already-open agent across polls without replaying its initial state."""

    def __init__(self, row):
        self.exit_at = row["finished"]
        self.announced = row.get("completed_turn") or row["done_turn"]
        self.ready = row.get("ready_turn")
        self.was_done = row_status(row) == "finished"

    def advanced(self, row, status):
        ready = row.get("ready_turn")
        marker = row["done_turn"]
        done = status == "finished"
        candidate = None
        if status not in ("running", "blocked", "exited"):
            if ready and (ready == self.ready or marker == ready or done):
                candidate = ready
            elif done and row.get("completed_turn"):
                candidate = row["completed_turn"]
            elif marker and (not row.get("native_turn") or marker == row.get("completed_turn")):
                candidate = marker
        # Older wrappers can publish just the unread/done badge. If a marker
        # arrives a poll later for that same badge, remember it without repeating it.
        new = bool(candidate and candidate != self.announced)
        if candidate:
            if self.was_done and not self.announced:
                new = False
            self.announced = candidate
        elif done and not self.was_done and not (row.get("native_turn") or marker):
            self.announced = None
            new = True
        self.ready, self.was_done = ready, done
        return new


def existing_notification_owners(tmux):
    """Managed wrappers launched with the current custom notifier deliver their own events."""
    route = os.environ.get("AGMUX_NOTIFY_CMD")
    if not route:
        return set()
    fmt = "#{session_id}\t#{@agmux_pane}\t#{AGMUX_NOTIFY_CMD}"
    owners = set()
    for line in tmux.run("list-sessions", "-F", fmt, optional=True).splitlines():
        fields = line.split("\t", 2)
        if len(fields) == 3 and fields[1] and fields[2] == route:
            owners.add(fields[0])
    return owners


def watch_command(tmux, interval, once=False, notify_enabled=True, existing=False):
    """Poll sessions and report when an agent starts waiting on the user."""
    previous = {}
    tracked = None
    while True:
        try:
            rows = tmux.session_details(detect=True)
            if existing and tracked is None:
                owners = existing_notification_owners(tmux) if notify_enabled else set()
        except Error as error:
            print(f"{APP}: {clean(error)}", file=sys.stderr)
            if once:
                return 1
            time.sleep(interval)
            continue
        if existing:
            current = {(r["created"], r["id"]): r for r in rows
                       if r["agent"] and r["agent"] != "shell"}
            if tracked is None:
                tracked = {key: WatchedCompletion(r) for key, r in current.items()
                           if r["id"] not in owners and (not r["finished"] or r["status"] == "running")}
            tracked = {key: events for key, events in tracked.items() if key in current}
            if not tracked:
                return 0
            rows = [current[key] for key in tracked]
        for row in rows:
            status = row_status(row)
            if existing:
                key = (row["created"], row["id"])
                watch = tracked[key]
                agent = AGENTS.get(row["agent"])
                label = agent.label if agent else "Agent"
                if row["finished"] and row["finished"] != watch.exit_at:
                    del tracked[key]
                    code = row["exit_code"]
                    if notify_enabled:
                        _run_notification(f"{label} closed" if row["agent"] == "codex" else f"{label} finished",
                                          f"{row['name']} (exit {code})", row["name"],
                                          int(code) if re.fullmatch(r"-?[0-9]{1,4}", code) else -1)
                elif watch.advanced(row, status) and notify_enabled:
                    _run_notification(f"{label} turn finished", row["name"], row["name"], 0)
            notice, previous[row["id"]] = blocked_transition(
                previous.get(row["id"]), status, row.get("prompt") or "waiting for input")
            if notice:
                agent = AGENTS.get(row.get("agent") or "")
                label = agent.label if agent and not agent.is_shell \
                    else (row.get("process") or "agent")
                print(f"{clean(row['name'])}: {clean(label)} needs input - {clean(notice)}")
                send_blocked_notification(label, row["name"], notice, notify_enabled)
        if once:
            return 0
        time.sleep(interval)


def build_parser():
    parser = argparse.ArgumentParser(prog=APP, description=__doc__)
    parser.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    parser.add_argument("--no-preview", action="store_true",
                        help="start with pane previews hidden")
    parser.add_argument("--no-notify", action="store_true", default=not notify_default(),
                        help="do not send a notification when an agent finishes")
    parser.add_argument("--no-resources", action="store_true",
                        help="do not sample per-session CPU and memory")
    sub = parser.add_subparsers(dest="command")
    pick = sub.add_parser("pick", help="search and select sessions (default)")
    pick.add_argument("--client", help=argparse.SUPPRESS)
    pick.add_argument("--no-preview", action="store_true", default=argparse.SUPPRESS,
                      help="start with pane previews hidden")
    listing = sub.add_parser("list", help="list running tmux sessions")
    listing.add_argument("--json", action="store_true",
                         help="emit one JSON array with CPU and memory per session")
    listing.add_argument("--no-resources", action="store_true", default=argparse.SUPPRESS,
                         help="do not sample per-session CPU and memory")
    sub.add_parser("ls", help="alias for list")
    sub.add_parser("save", help="save all live workspaces now")
    saved = sub.add_parser("saved", help="list saved workspaces available for recovery")
    saved.add_argument("--json", action="store_true", help="emit saved workspaces as JSON")
    restore = sub.add_parser("restore", help="restore saved workspaces, detached")
    restore.add_argument("session", nargs="?")
    restore.add_argument("--all", action="store_true", help="restore all missing saved workspaces")
    restore.add_argument("--fresh", action="store_true",
                         help="replay saved agent launch arguments instead of opening history/shells")
    restore.add_argument("--no-notify", action="store_true", default=argparse.SUPPRESS)
    forget = sub.add_parser("forget", help="remove a saved definition without killing a live session")
    forget.add_argument("session")
    forget.add_argument("--yes", action="store_true", help="skip typing the saved session name")
    info = sub.add_parser("info", help="show details for one session")
    info.add_argument("session")
    info.add_argument("--json", action="store_true", help="emit one JSON object")
    info.add_argument("--no-resources", action="store_true", default=argparse.SUPPRESS,
                      help="do not sample CPU and memory")
    sub.add_parser("names", help="print session names for shell completion")
    attach = sub.add_parser("attach", help="attach outside tmux; switch inside it")
    attach.add_argument("session")
    new = sub.add_parser("new", help="start an agent or shell in a named session")
    new.add_argument("name", nargs="?", help="omit to name the session from its first message")
    new.add_argument("--agent", default=DEFAULT_AGENT,
                     help=f"agent to run (default: {DEFAULT_AGENT}); 'shell' for a plain session")
    new.add_argument("--cwd", default=str(Path.cwd()))
    new.add_argument("--shell", action="store_true", help="alias for --agent shell")
    new.add_argument("--detach", action="store_true", help="create without attaching")
    new.add_argument("--no-notify", action="store_true", default=argparse.SUPPRESS,
                     help="no completion notification")
    rename = sub.add_parser("rename", help="rename an existing session")
    rename.add_argument("session")
    rename.add_argument("name")
    shell = sub.add_parser("shell", help="add a detached shell window to a session")
    shell.add_argument("session")
    shell.add_argument("--name", help="name for the new window")
    shell.add_argument("--cwd", help="directory for the new window (default: active pane)")
    delete = sub.add_parser("delete", help="end a session after confirmation")
    delete.add_argument("session")
    delete.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation for this one session")
    sub.add_parser("kill", help="alias for delete").add_argument("session")
    usage = sub.add_parser("usage", help="show locally available provider usage limits")
    usage.add_argument("--json", action="store_true",
                       help="emit Codex limits and OpenCode session usage as JSON")
    sub.add_parser("agents", help="list known agents and whether they are installed")
    watch = sub.add_parser("watch", help="notify when an agent needs input")
    watch.add_argument("--interval", type=float, default=watch_interval(),
                       help="seconds between checks (default: %(default)s)")
    watch.add_argument("--once", action="store_true", help="check once and exit")
    watch.add_argument("--existing", action="store_true",
                       help="watch only agents open now, including newly recorded completions, then exit")
    watch.add_argument("--no-notify", action="store_true", default=argparse.SUPPRESS,
                       help="print changes without desktop notifications")
    discord = sub.add_parser("discord", help="configure or test outgoing Discord alerts")
    modes = discord.add_mutually_exclusive_group()
    modes.add_argument("--setup", action="store_true", help="save a webhook using a hidden prompt")
    modes.add_argument("--test", action="store_true", help="send a test notification")
    discord.add_argument("--quiet-completions", action="store_true",
                         help="silence successful completion alerts in Discord")
    discord.add_argument("title", nargs="?")
    discord.add_argument("session", nargs="?")
    discord.add_argument("exit_code", nargs="?", type=int)
    preview = sub.add_parser("preview", help="show recent output from the active pane")
    preview.add_argument("session")
    peek = sub.add_parser("peek", help="read-only live output viewer without attaching")
    peek.add_argument("session")
    sub.add_parser("detach", help="detach the current tmux client")
    sub.add_parser("last", help="switch back to the previous session inside tmux")
    sub.add_parser("doctor", help="check tools and integration")
    return parser


def main(argv=None):
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "_save-loop":
        internal = argparse.ArgumentParser(add_help=False)
        internal.add_argument("--socket", required=True)
        internal.add_argument("--state-dir", required=True)
        internal.add_argument("--server", required=True)
        options = internal.parse_args(args_list[1:])
        return workspace_saver(options.socket, options.state_dir, options.server)
    if args_list and args_list[0] == "_launch":
        internal = argparse.ArgumentParser(add_help=False)
        internal.add_argument("--agent", required=True)
        internal.add_argument("--bin", required=True)
        internal.add_argument("--shell", required=True)
        internal.add_argument("--session", required=True)
        internal.add_argument("--argv", required=True)
        internal.add_argument("--notify", dest="notify", action="store_true")
        internal.add_argument("--no-notify", dest="notify", action="store_false")
        internal.set_defaults(notify=notify_default())
        options = internal.parse_args(args_list[1:])
        try:
            arguments = json.loads(options.argv)
        except ValueError:
            raise Error("Invalid agent argument list.") from None
        if not isinstance(arguments, list) or not all(isinstance(a, str) for a in arguments):
            raise Error("Agent arguments must be a list of strings.")
        return launch_agent(options.agent, options.bin, options.shell, options.session,
                            arguments, options.notify)

    parser = build_parser()
    agent_args = []
    # Skip leading global flags so a `new ... -- args` split still finds the
    # subcommand even when it is not the first token.
    index = 0
    globals_ = ("--no-preview", "--no-notify", "--no-resources")
    while index < len(args_list) and args_list[index] in globals_:
        index += 1
    if args_list[index:index + 1] == ["new"] and "--" in args_list[index:]:
        divider = args_list.index("--", index)
        agent_args, args_list = args_list[divider + 1:], args_list[:divider]
    args = parser.parse_args(args_list)
    if args.command == "discord":
        from agmux_discord import run
        if args.test and args.title is None and os.environ.get("AGMUX_NOTIFY_CMD"):
            try:
                route = notification_argv(os.environ["AGMUX_NOTIFY_CMD"], "agmux completion test",
                                          "Test session completed", "test session", 0)
            except (ValueError, KeyError, IndexError):
                raise Error("Invalid notify_cmd quoting or placeholder. Check your agmux config.") from None
            if route[:2] == [APP, "discord"]:
                delivery = parser.parse_args(route[1:])
                if delivery.setup or delivery.test or delivery.session is None:
                    raise Error("Discord notify_cmd must take an event title and session name.")
                result = run(delivery, parser)
                if result == 0:
                    print("Discord accepted a completion test through the configured notification command.")
                return result
        return run(args, parser)
    if args.command == "agents":
        for agent in AGENTS.values():
            if agent.is_shell:
                print(f"{agent.label:<9} plain session")
            else:
                print(f"{agent.label:<9} {shutil.which(agent.binary) or 'not installed'}")
        return 0
    if not shutil.which("tmux"):
        raise Error("tmux is not installed.")
    tmux = Tmux()
    resources = resources_enabled() and not args.no_resources
    if args.command in (None, "pick", "new", "rename", "shell", "delete", "kill", "watch"):
        tmux.autosave()
        if tmux.recovery_error and args.command not in (None, "pick"):
            print(f"{APP}: {tmux.recovery_error}", file=sys.stderr)
    if args.command == "save":
        tmux.checkpoint()
        if recovery_enabled():
            tmux.start_saver()
        print(f"Saved {len(tmux.sessions())} live workspace(s) to {tmux.workspace_store().path}")
        return 0
    if args.command == "saved":
        rows = saved_rows(tmux, explicit=True)
        if args.json:
            print(json.dumps(rows))
        else:
            for row in rows:
                print(f"{clean(row['name'])}  {row['agent']}  {row['windows']}w  "
                      f"{clean(short_path(row['cwd']))}  saved {humanize_age(row['activity'])}")
        return 0
    if args.command == "forget":
        if not args.yes:
            if not sys.stdin.isatty():
                raise Error("Confirmation needs a terminal; pass --yes for this saved session.")
            answer = input(f"Type {clean(args.session)!r} to forget its saved workspace: ")
            if answer != args.session:
                print("Cancelled.")
                return 0
        tmux.workspace_store().forget(args.session)
        print(f"Forgot saved workspace {clean(args.session)}.")
        return 0
    if args.command == "restore":
        if bool(args.session) == args.all:
            raise Error("Choose one saved session name or --all.")
        targets = [row["name"] for row in saved_rows(tmux, explicit=True)] if args.all else [args.session]
        failures = []
        for target in targets:
            try:
                tmux.restore(target, fresh=args.fresh, notify=not args.no_notify)
                print(f"Restored {clean(target)}. Attach with: {APP} attach {shlex.quote(target)}")
            except (Error, OSError) as error:
                failures.append(f"{clean(target)}: {clean(error)}")
        if failures:
            raise Error("; ".join(failures))
        if not targets:
            print("No saved workspaces need restoring.")
        return 0
    if args.command in (None, "pick"):
        return interactive(tmux, getattr(args, "client", None),
                           show_preview=not args.no_preview,
                           notify=not args.no_notify, resources=resources)
    if args.command in ("list", "ls"):
        rows = tmux.session_details(detect=True, resources=resources)
        if getattr(args, "json", False):
            print(json.dumps([session_record(row) for row in rows]))
        else:
            enabled = color_enabled()
            for row in rows:
                print(list_line(row, enabled))
    elif args.command == "info":
        enabled = color_enabled()
        record = info_record(tmux, args.session, resources=resources)
        if args.json:
            print(json.dumps(record))
        else:
            for line in info_lines(record, enabled):
                print(line)
    elif args.command == "names":
        for row in tmux.sessions():
            print(clean(row["name"]))
    elif args.command == "attach":
        return tmux.connect(tmux.resolve(args.session)["id"])
    elif args.command == "new":
        if args.shell:
            args.agent = "shell"
        if args.agent == "shell" and agent_args:
            raise Error("Agent arguments cannot be used with a shell session.")
        if args.agent != "shell" and args.agent not in AGENTS:
            raise Error(f"Unknown agent {clean(args.agent)!r}. Known: {', '.join(AGENTS)}")
        # Check terminal access before creating an otherwise unreachable session.
        if not args.detach and not os.environ.get("TMUX") and not sys.stdin.isatty():
            raise Error("Use --detach when creating a session without a terminal.")
        client = tmux.client() if not args.detach and os.environ.get("TMUX") else None
        auto_name = args.name is None
        if auto_name:
            args.name = tmux.suggested_name(args.cwd, args.agent)
        sid = tmux.create(args.name, args.cwd, args.agent, agent_args,
                          notify=not args.no_notify, auto_name=auto_name)
        if args.detach:
            print(f"Created {args.name}. Attach with: {APP} attach {shlex.quote(args.name)}")
        else:
            return tmux.connect(sid, client)
    elif args.command == "rename":
        tmux.rename(tmux.resolve(args.session)["id"], args.name)
    elif args.command == "shell":
        enabled = color_enabled()
        sid = tmux.resolve(args.session)["id"]
        index = tmux.shell_window(sid, args.cwd, args.name)
        print(paint(f"Added shell window {index} to {args.session}.", "attached", enabled)
              + " Select it with Ctrl-b then w (or Ctrl-b w).")
    elif args.command in ("delete", "kill"):
        enabled = color_enabled()
        info = tmux.describe(tmux.resolve(args.session)["id"])
        print(paint(f"Session {info['name']}  ({info['id']})", "heading", enabled))
        print(f"Windows: {info['windows']}   Panes: {info['pane_count']}   "
              f"Attached clients: {info['client_count']}")
        for pane in info["panes"]:
            marker = "*" if pane["active"] else " "
            print(f"  {marker} window {pane['window']}: {clean(pane['command'])}  "
                  f"{paint(short_path(pane['path']), 'muted', enabled)}")
        print(paint("Ending this live session stops its running programs.", "warning", enabled))
        print("It does not delete any files in your project directories.")
        if not args.yes:
            if not sys.stdin.isatty():
                raise Error("Confirmation needs a terminal; pass --yes for this one session.")
            print("Type the session name to confirm, or press Enter to cancel:")
            try:
                answer = sys.stdin.readline()
            except KeyboardInterrupt:
                print()
                answer = ""
            if not confirm_delete_answer(info["name"], answer):
                print("Cancelled. Nothing was deleted.")
                return 0
        tmux.delete(info["id"], expected=info)
        print(paint(f"Deleted session {info['name']}.", "attached", enabled))
    elif args.command == "usage":
        if args.json:
            print(json.dumps(dict(codex=codex_usage(0), opencode=opencode_usage(0)),
                             sort_keys=True))
        else:
            enabled = color_enabled()
            for text, role in usage_report(width=24):
                print(paint(text, role, enabled))
    elif args.command == "preview":
        enabled = color_enabled()
        sid = tmux.resolve(args.session)["id"]
        agent = next((row.get("agent") for row in tmux.session_details(detect=True)
                      if row["id"] == sid), None)
        lines = tmux.preview(sid, ansi=enabled)
        if lines:
            print(clean(lines[0]))
        for line in render_ansi(trim_chrome(agent, lines[2:]), enabled):
            print(line)
    elif args.command == "watch":
        return watch_command(tmux, args.interval, once=args.once,
                             notify_enabled=not args.no_notify, existing=args.existing)
    elif args.command == "peek":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise Error(f"The viewer requires a terminal. Use {APP} preview for text output.")
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        curses.wrapper(viewer, tmux, tmux.resolve(args.session))
    elif args.command in ("detach", "last"):
        if not os.environ.get("TMUX"):
            raise Error(f"Run {APP} {args.command} from inside tmux.")
        client = tmux.client()
        if args.command == "last":
            tmux.run("switch-client", "-l", "-c", client)
        else:
            tmux.run("detach-client", "-t", client)
    elif args.command == "doctor":
        doctor(tmux)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, OSError, curses.error) as error:
        print(f"{paint(APP + ':', 'warning', color_enabled(sys.stderr))} {clean(error)}",
              file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
