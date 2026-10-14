#!/usr/bin/env python3
"""agmux - a dependency-free tmux session manager for AI coding agents.

One standard-library Python file. agmux lists and manages tmux sessions, starts
agent sessions (Codex, OpenCode, Claude Code, ...) or plain shells, flags
sessions whose agent has finished, and shows what local provider usage data it
can find. It never makes network requests and never reads credential files.
"""

import argparse
import curses
import json
import locale
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time


APP = "agmux"
VERSION = "0.3.0"
SELF = Path(__file__).resolve()


def _env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


ROOT = Path(_env("AGMUX_REPOS", "CX_REPOS", default=str(Path.home() / "source/repos")))
CODEX_HOME = Path(_env("CODEX_HOME", default=str(Path.home() / ".codex")))
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


def _pick_glyphs():
    if os.environ.get("AGMUX_ASCII"):
        return {"running": ">", "finished": "*", "blocked": "!", "idle": ".",
                "shell": ".", "bar_full": "#", "bar_empty": "-", "sep": "-", "dot": "*"}
    encoding = sys.stdout.encoding or "utf-8"
    try:
        "\u25b6\u2713\u00b7\u2588\u2591\u2500\u25cf\u203a".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return {"running": ">", "finished": "*", "blocked": "!", "idle": ".",
                "shell": ".", "bar_full": "#", "bar_empty": "-", "sep": "-", "dot": "*"}
    return {"running": "\u25b6", "finished": "\u2713", "blocked": "!",
            "idle": "\u203a", "shell": "\u00b7", "bar_full": "\u2588",
            "bar_empty": "\u2591", "sep": "\u2500", "dot": "\u25cf"}


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
_BLOCKED_PATTERNS = [
    r"\[y/n\]", r"\(y/n\)", r"\[Y/n\]", r"\[y/N\]", r"\byes/no\b",
    r"\ballow\b[^.]*\b(run|command|access|edit|write|execute|this|once|always)\b",
    r"\b(approve|approval|permit|permission)\b",
    r"\bwould you like to\b", r"\bdo you want to\b", r"\bare you sure\b",
    r"\bproceed\?", r"\bcontinue\?", r"\boverwrite\?", r"\bconfirm\?",
    r"\bpress (enter|y)\b[^.]*\b(confirm|continue|approve|allow|deny)",
    r"\bescalat", r"\bneeds? (your )?(input|approval|permission)\b",
    r"^\s*(?:[>\u276f]|\d+[.)])\s*(?:yes|allow|approve|deny|no)\b",
    r"\b(would|do|does|shall|should|can|could|may)\b[^?]{0,120}\?\s*$",
]
_WORKING_PATTERNS = [
    r"esc to interrupt", r"\besc\b[^.]*\binterrupt\b", r"ctrl\+c to (cancel|stop|interrupt)",
    r"press esc[^.]*\b(stop|cancel|interrupt)\b", r"working\s*(?:\(|\u2022|\.\.\.)",
    r"thinking\s*(?:\(|\u2022|\.\.\.)", r"esc replaces",
]


def _compile_patterns(defaults, extra_env):
    patterns = list(defaults)
    extra = os.environ.get(extra_env)
    if extra:
        patterns.extend(part for part in extra.split("|") if part)
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return compiled


def classify_screen(lines, limit=24):
    """Return (state, prompt) where state is blocked, working, or idle.

    ``prompt`` is the screen line that looked like it needs the user.
    """
    tail = [line.rstrip() for line in lines[-limit:] if line.strip()]
    blocked = _compile_patterns(_BLOCKED_PATTERNS, "AGMUX_BLOCKED_PATTERNS")
    working = _compile_patterns(_WORKING_PATTERNS, "AGMUX_WORKING_PATTERNS")
    for line in reversed(tail):
        for pattern in blocked:
            if pattern.search(line):
                return "blocked", line.strip()[:200]
    joined = "\n".join(tail)
    for pattern in working:
        if pattern.search(joined):
            return "working", ""
    return "idle", ""


_BOX_CHARS = set(chr(code) for code in range(0x2500, 0x25A0)) | \
    {" ", ".", "\u00b7", "\u2b1d"}

# Agents draw a fixed input composer and status footer at the bottom of their
# TUIs. It is the same every time and hides real output, so previews drop it.
_CODEX_COMPOSER = re.compile(r"^[\u203a\u276f>]\s")
_CODEX_STATUS = re.compile(r"\b(?:xhigh|high|medium|low|minimal)\b\s*\u00b7")


def _classify_chrome(agent, line):
    """Return 'strong', 'weak', or None for one trailing preview line."""
    text = line.rstrip()
    stripped = text.strip()
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
        limits = record.get("payload", {}).get("rate_limits")
        if limits:
            return limits
    return None


def find_codex_rate_limits():
    sessions = CODEX_HOME / "sessions"
    if not sessions.is_dir():
        return None
    try:
        files = sorted(sessions.rglob("rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:8]
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
    return " \u00b7 ".join(parts) if len(parts) > 1 else ""


def usage_report(width=20, limits=None):
    """(text, role) lines describing locally available usage limits."""
    limits = codex_usage() if limits is None else limits
    if not limits:
        return [("No local usage data yet - start a Codex session to populate it.", "muted")]
    lines = [(f"Codex - {limits.get('plan_type') or 'unknown'} plan", "heading")]
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
    return lines


# --------------------------------------------------------------------------
# tmux
# --------------------------------------------------------------------------

_SESSION_FIELDS = ("session_id", "session_name", "session_windows", "session_attached",
                   "session_activity", "session_created",
                   "@agmux_agent", "@agmux_status", "@agmux_unread", "@agmux_finished")


class Tmux:
    def __init__(self, socket=None):
        socket = socket or _env("AGMUX_TMUX_SOCKET", "CX_TMUX_SOCKET")
        if not socket and os.environ.get("TMUX"):
            socket = os.environ["TMUX"].split(",", 1)[0]
        executable = shutil.which("tmux")
        if not executable:
            raise Error("tmux is not installed.")
        self.command = [executable] + (["-S", socket] if socket else [])

    def run(self, *args, optional=False):
        try:
            result = subprocess.run(self.command + list(args), text=True, errors="replace",
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        except subprocess.TimeoutExpired:
            raise Error("tmux did not respond within five seconds.") from None
        if result.returncode:
            missing = ("no server running", "no sessions", "no such file or directory",
                       "connection refused")
            if optional and any(s in result.stderr.lower() for s in missing):
                return ""
            raise Error(result.stderr.strip() or "tmux command failed")
        return result.stdout.rstrip("\n")

    def sessions(self):
        fmt = "\t".join("#{" + key + "}" for key in _SESSION_FIELDS)
        rows = []
        for line in self.run("list-sessions", "-F", fmt, optional=True).splitlines():
            fields = line.split("\t")
            if len(fields) != len(_SESSION_FIELDS):
                continue
            (sid, name, windows, attached, activity, created,
             agent, status, unread, finished) = fields
            rows.append(dict(id=sid, name=name, windows=int(windows), attached=int(attached),
                             activity=int(activity or 0), created=int(created or 0),
                             agent=agent or "", status=status or "",
                             unread=int(unread or 0), finished=int(finished or 0)))
        return sorted(rows, key=lambda row: (-row["activity"], row["name"]))

    def session_details(self, detect=False):
        rows = self.sessions()
        if not rows:
            return []
        fmt = "\t".join("#{" + key + "}" for key in
                        ("session_id", "window_active", "pane_active",
                         "pane_current_command", "pane_current_path", "pane_id"))
        active = {}
        for line in self.run("list-panes", "-a", "-F", fmt, optional=True).splitlines():
            fields = line.split("\t", 5)
            if len(fields) == 6 and fields[1:3] == ["1", "1"]:
                active[fields[0]] = dict(process=fields[3], cwd=fields[4], pane=fields[5])
        for row in rows:
            info = active.get(row["id"], {"process": "", "cwd": "", "pane": ""})
            row["process"] = info["process"]
            row["cwd"] = info["cwd"]
            row["pane"] = info["pane"]
            row["state"] = ""
            row["prompt"] = ""
            if not row["agent"]:
                detected = detect_agent(row["process"])
                row["agent"] = detected.key if detected else ""
            if (detect and row["agent"] and row["agent"] != "shell" and row["pane"]
                    and row["process"] not in SHELL_COMMANDS):
                try:
                    state, prompt = classify_screen(self.pane_lines(row["pane"]))
                except Error:
                    state, prompt = "", ""
                row["state"], row["prompt"] = state, prompt
        return rows

    def pane_lines(self, target, lines=24):
        """Visible screen text of a pane (target may be a pane id or 'session:')."""
        return self.run("capture-pane", "-p", "-t", target, "-S", f"-{lines}").splitlines()

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
        # leaves a full preview of real content.
        depth = height + 12
        output = self.run("capture-pane", "-p", *(["-e"] if ansi else []),
                          "-t", active[0], "-S", f"-{depth}")
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

    def mark_read(self, sid):
        try:
            self.session_option(sid, "agmux_unread", "0")
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

    def create(self, name, cwd, agent=DEFAULT_AGENT, agent_args=(), notify=True):
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
        if not profile.is_shell:
            binary = shutil.which(profile.binary)
            if not binary:
                raise Error(f"{profile.binary} is not on PATH. Use --agent shell for a plain session.")
            user_shell = os.environ.get("SHELL", "/bin/bash")
            if not Path(user_shell).is_file() or not os.access(user_shell, os.X_OK):
                user_shell = "/bin/bash"
            # tmux executes this argv directly. JSON is one literal argument, even
            # when agent arguments contain tmux separators or shell metacharacters.
            args += [sys.executable, str(SELF), "_launch",
                     "--agent", profile.key, "--bin", binary, "--shell", user_shell,
                     "--session", name, "--notify" if notify else "--no-notify",
                     "--argv", json.dumps(list(agent_args))]
        sid = self.run(*args)
        try:
            self.session_option(sid, "agmux_agent", profile.key)
            self.session_option(sid, "agmux_status", "shell" if profile.is_shell else "running")
            self.session_option(sid, "agmux_started", str(int(time.time())))
        except Error:
            pass
        return sid

    def rename(self, sid, name):
        validate_name(name)
        self.run("rename-session", "-t", sid, name)

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
        return self.run(*args)


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


def repos():
    if not ROOT.is_dir():
        raise Error(f"Repository directory does not exist: {ROOT}. Set AGMUX_REPOS to change it.")
    return [dict(id=str(p), name=p.name) for p in sorted(ROOT.iterdir()) if p.is_dir()]


def short_path(directory):
    prefix = str(Path.home()) + os.sep
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
    prompt rather than working.
    """
    if row.get("unread") or row.get("status") == "finished":
        return "finished"
    state = row.get("state")
    if state == "blocked":
        return "blocked"
    if state == "working":
        return "running"
    if state == "idle":
        return "idle" if row.get("agent") and row["agent"] != "shell" else "shell"
    if row.get("status") == "running":
        return "running"
    if row.get("agent") and row["agent"] != "shell":
        return "running"
    return "shell"


_STATUS_ORDER = {"blocked": 0, "finished": 1, "running": 2, "idle": 3, "shell": 4}


def state_priority(row):
    return _STATUS_ORDER.get(row_status(row), 5)


# --------------------------------------------------------------------------
# Notifications and the agent launch wrapper
# --------------------------------------------------------------------------


def notify_default():
    value = os.environ.get("AGMUX_NOTIFY", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _run_notification(title, body, session, exit_code=-1):
    """Best-effort desktop/terminal notice. Never blocks or raises."""
    custom = os.environ.get("AGMUX_NOTIFY_CMD")
    if custom:
        try:
            parts = [part.format(title=title, body=body, session=session,
                                 exit_code=exit_code) for part in shlex.split(custom)]
            if parts:
                subprocess.Popen(parts, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except (ValueError, OSError):
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
        return max(1.0, float(os.environ.get("AGMUX_WATCH_INTERVAL", "3") or 3))
    except ValueError:
        return 3.0


def watch_blocked(tmux, sid, label, session, stop_event, interval):
    """While an agent runs, poll its pane and notify on a new blocked prompt."""
    last_prompt = None
    while not stop_event.wait(interval):
        try:
            lines = tmux.pane_lines(f"{sid}:")
        except Error:
            continue
        state, prompt = classify_screen(lines)
        if state == "blocked" and prompt and prompt != last_prompt:
            last_prompt = prompt
            send_blocked_notification(label, session, prompt)


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
        tmux = Tmux()
        sid = tmux.current_session_id()
    except Error:
        pass

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

    started = time.time()
    code = 0
    stop_event = threading.Event()
    if tmux and sid and notify_enabled:
        threading.Thread(target=watch_blocked,
                         args=(tmux, sid, profile.label, session, stop_event, watch_interval()),
                         daemon=True).start()
    try:
        child = subprocess.Popen([executable, *arguments])
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

    elapsed = time.time() - started
    set_option("agmux_status", "finished")
    set_option("agmux_exit", str(code))
    set_option("agmux_finished", str(int(time.time())))
    set_option("agmux_duration", str(int(elapsed)))
    set_option("agmux_unread", "1")

    send_notification(profile.label, session, code, elapsed, notify_enabled)
    if tmux and sid:
        try:
            tmux.run("display-message", "-d", "8000",
                     f"{APP}: {profile.label} finished in {session} "
                     f"({humanize_delta(elapsed)})", optional=True)
        except Error:
            pass
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


def prompt(screen, label, initial="", style=None):
    style = style or Style()
    value = initial
    screen.timeout(-1)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    while True:
        height, _ = screen.getmaxyx()
        screen.move(max(0, height - 2), 0)
        screen.clrtoeol()
        put(screen, height - 2, 0, f"{label}: {value}", style.heading)
        screen.refresh()
        key = screen.get_wch()
        if key in ("\n", "\r", curses.KEY_ENTER):
            break
        if key in ("\x1b", "\x03"):
            value = None
            break
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            value = value[:-1]
        elif key == "\x15":
            value = ""
        elif isinstance(key, str) and key.isprintable():
            value += key
    screen.timeout(200)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    return value


def viewer(screen, tmux, row, style=None):
    """Read tmux's existing output; never attach or forward input to a pane."""
    style = style or Style()
    renderer = AnsiRenderer()
    offset, refreshed, body, caption = 0, 0.0, [], ""
    screen.timeout(200)
    while True:
        height, width = screen.getmaxyx()
        now = time.monotonic()
        if now - refreshed > 2:
            try:
                lines = tmux.preview(row["id"], 500, ansi=True)
                caption = lines[0] if lines else ""
                content = trim_chrome(row.get("agent"), lines[2:])
                body = [parse_sgr(line) for line in content]
            except Error as error:
                caption, body = str(error), []
            refreshed = now
        visible = max(1, height - 5)
        offset = max(0, min(offset, max(0, len(body) - visible)))
        start = max(0, len(body) - visible - offset)
        screen.erase()
        put(screen, 0, 0, f"Read-only output: {row['name']}", style.heading)
        put(screen, 1, 0, "Up/Down/PageUp/PageDown scroll | Home oldest | End live | Esc back",
            style.help)
        put(screen, 2, 0, caption, style.muted)
        for index, runs in enumerate(body[start:start + visible]):
            put_runs(screen, 3 + index, runs, renderer)
        put(screen, height - 1, 0, "Live tail" if not offset else f"{offset} lines above live tail",
            style.muted)
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", "\x03"):
            return
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


def show_help(screen, style=None):
    style = style or Style()
    lines = [f"{APP} - keyboard help", "",
             "Type: search session names, agents, commands, and directories",
             "Up/Down or Tab/Shift-Tab: select a session",
             "PageUp/PageDown, Home/End: move through the list",
             "Enter: attach or switch your terminal to the selected session",
             "Ctrl-N: start a new agent or shell session (a short wizard)",
             "Ctrl-G: show locally available usage limits",
             "Ctrl-R: rename the selected session",
             "Ctrl-D or Delete: delete the selected session (asks you to confirm)",
             "Ctrl-W: add a detached shell window in the session's directory",
             "Ctrl-O: read-only output viewer (your keys stay in the viewer)",
             "Ctrl-P: show/hide pane preview",
             "Ctrl-L: refresh immediately; Ctrl-U: clear the search",
             "Esc: cancel; F1: help", "",
             "Sessions whose agent needs input are flagged and sorted first.",
             "Sessions whose agent finishes get a check mark and a notification.",
             "Previews and the viewer show the pane's own colors and styles.",
             "Deleting ends a live session and its programs; project files stay on disk.",
             "This helper reads local tmux output, directory names, and Codex usage",
             "logs. It has no telemetry and makes no network requests.", "",
             "Esc, Enter, or F1 returns to the picker."]
    screen.erase()
    for index, line in enumerate(lines):
        put(screen, index, 0, line, style.heading if index == 0 else style.help)
    screen.refresh()
    while True:
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", "\x03", "\n", "\r", curses.KEY_ENTER, curses.KEY_F1):
            return


def show_usage_panel(screen, style=None):
    style = style or Style()
    width = max(10, min(28, screen.getmaxyx()[1] - 24))
    screen.timeout(-1)
    screen.erase()
    put(screen, 0, 0, f"{APP} usage limits", style.heading)
    put(screen, 1, 0, "Read locally from Codex session logs. No network requests.",
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
    """Red confirmation screen; requires typing the exact name.

    Returns "deleted", "cancelled", or "changed". On "changed" nothing was
    deleted and the caller should ask the user to start confirmation again.
    """
    style = style or Style()
    typed, message, result = "", "", "cancelled"
    screen.timeout(-1)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
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
            put(screen, height - 5, 0, "Type the exact name below, or press Esc to cancel.",
                style.help)
        put(screen, height - 2, 0, f"> {typed}", style.warning)
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", "\x03"):
            result = "cancelled"
            break
        if key in ("\n", "\r", curses.KEY_ENTER):
            if not confirm_delete_answer(info["name"], typed):
                message = "Name did not match. Type it exactly, or Esc to cancel."
                typed = ""
                continue
            try:
                tmux.delete(info["id"], expected=info)
                result = "deleted"
            except Error as error:
                message = str(error)
                result = "changed"
            break
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            typed, message = typed[:-1], ""
        elif key == "\x15":
            typed, message = "", ""
        elif isinstance(key, str) and key.isprintable():
            typed += key
            message = ""
    screen.timeout(200)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    return result


def draw_row(screen, y, row, selected, style):
    status = row_status(row)
    glyph = {"finished": GLYPH["finished"], "running": GLYPH["running"],
             "blocked": GLYPH["blocked"], "idle": GLYPH["idle"]}.get(status, GLYPH["shell"])
    glyph_role = {"finished": "finished", "running": "attached",
                  "blocked": "warning", "idle": "muted"}.get(status, "muted")
    segments = [(glyph + " ", glyph_role), (row["name"], "name")]
    agent = AGENTS.get(row.get("agent") or "")
    if agent and not agent.is_shell:
        segments.append(("  " + agent.label, agent.accent))
    if status == "blocked":
        segments.append(("  needs input", "warning"))
    segments.append((f"  {row['windows']}w", "muted"))
    if row["attached"]:
        segments.append(("  attached", "attached"))
    if row.get("process"):
        segments.append(("  " + clean(row["process"]), "default"))
    if row.get("cwd"):
        segments.append(("  " + short_path(row["cwd"]), "muted"))
    age = humanize_age(row.get("activity"))
    if age:
        segments.append(("  " + age, "muted"))
    if selected:
        put(screen, y, 0, "".join(text for text, _ in segments), style.selected)
        return
    x = 0
    for text, role in segments:
        text = clean(text)
        put(screen, y, x, text, getattr(style, role, 0))
        x += len(text)


def choose(screen, title, options, style, hint="Up/Down select | Enter confirm | Esc cancel"):
    """A small searchable list chooser. Returns the selected value or None."""
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
        selection = max(0, min(selection, len(matches) - 1))
        height, width = screen.getmaxyx()
        screen.erase()
        put(screen, 0, 0, title, style.heading)
        put(screen, 1, 0, hint, style.help)
        put(screen, 2, 0, f"Search: {query}", style.heading)
        visible = max(1, height - 5)
        start = max(0, selection - visible + 1)
        for index, option in enumerate(matches[start:start + visible], start):
            text = ("> " if index == selection else "  ") + option["label"]
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
            return matches[selection].get("value", matches[selection]["label"])
        if key in (curses.KEY_DOWN, "\t"):
            selection = (selection + 1) % max(1, len(matches))
        elif key in (curses.KEY_UP, curses.KEY_BTAB):
            selection = (selection - 1) % max(1, len(matches))
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            query = query[:-1]
        elif key == "\x15":
            query, selection = "", 0
        elif isinstance(key, str) and key.isprintable():
            query += key


def new_session_flow(screen, tmux, style, notify=True):
    """Ctrl-N wizard: choose an agent, a directory, then a name."""
    options = []
    for agent in AGENTS.values():
        if agent.is_shell:
            options.append(dict(label="Shell", note="a plain tmux session",
                                value="shell", enabled=True))
        elif shutil.which(agent.binary):
            options.append(dict(label=agent.label, note=f"start {agent.binary}",
                                value=agent.key, enabled=True))
    agent = choose(screen, "New session - choose what to run", options, style)
    if agent is None:
        return None

    directories = [dict(label=str(Path.cwd()), note="current directory",
                        value=str(Path.cwd()))]
    try:
        for repo in repos():
            directories.append(dict(label=repo["name"], note=short_path(repo["id"]),
                                    value=repo["id"]))
    except Error:
        pass
    directories.append(dict(label="~", note="home directory", value=str(Path.home())))
    cwd = choose(screen, f"New {AGENTS[agent].label} session - choose a directory",
                 directories, style)
    if cwd is None:
        return None

    suggestion = default_name(cwd, tmux.sessions(), agent)
    name = prompt(screen, "Session name (Ctrl-U clears)", suggestion, style)
    if name is None:
        return None
    sid = tmux.create(name, cwd, agent=agent, notify=notify)
    return dict(id=sid, name=name)


def picker(screen, tmux, show_preview=True, style=None, notify=True):
    style = style or setup_styles()
    screen.keypad(True)
    screen.timeout(200)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    query, selection, selected_id = "", 0, None
    rows, refreshed, message, message_alert = [], 0.0, "", False
    preview_key, preview_at = None, 0.0
    preview_caption, preview_body = "", []
    renderer = AnsiRenderer()
    while True:
        now = time.monotonic()
        if now - refreshed > 2:
            try:
                rows = tmux.session_details(detect=True)
            except Error as error:
                rows, message, message_alert = [], str(error), True
            refreshed = now
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
        statuses = [row_status(row) for row in rows]
        running = statuses.count("running")
        finished = statuses.count("finished")
        blocked = statuses.count("blocked")
        summary = f"{len(rows)} sessions \u00b7 {running} running"
        if blocked:
            summary += f" \u00b7 {blocked} need input"
        summary += f" \u00b7 {finished} done"
        put(screen, 0, 0, f"{APP} {VERSION}", style.heading)
        put_right(screen, 0, summary, style.warning if blocked else style.muted)
        usage = usage_inline()
        if usage:
            put(screen, 1, 0, usage, style.muted)
            put_right(screen, 1, "Ctrl-G details", style.help)
        else:
            put(screen, 1, 0, "Ctrl-N new session   Ctrl-G usage   F1 help", style.help)
        put(screen, 2, 0, f"Search: {query}", style.heading)
        list_top = 3
        list_height = max(1, (height - list_top - 2) // 2) if show_preview \
            else max(1, height - list_top - 2)
        start = max(0, selection - list_height + 1)
        for index, row in enumerate(matches[start:start + list_height], start):
            draw_row(screen, list_top + index - start, row, index == selection, style)
        if not matches:
            put(screen, list_top, 0,
                "No matching sessions. Ctrl-N starts one.", style.muted)
        if show_preview:
            preview_y = list_top + list_height + 1
            put(screen, preview_y - 1, 0, GLYPH["sep"] * (width - 1), style.muted)
            if selected:
                if selected_id != preview_key or now - preview_at > 2:
                    try:
                        lines = tmux.preview(selected_id, max(1, height - preview_y - 6),
                                             ansi=True)
                        preview_caption = lines[0] if lines else ""
                        content = trim_chrome(selected.get("agent"), lines[2:])
                        preview_body = [parse_sgr(line) for line in content]
                    except Error as error:
                        preview_caption, preview_body = str(error), []
                    preview_key, preview_at = selected_id, now
                put(screen, preview_y, 0, preview_caption, style.muted)
                offset = 1
                if row_status(selected) == "blocked" and selected.get("prompt"):
                    put(screen, preview_y + offset, 0,
                        "! needs input: " + selected["prompt"], style.warning)
                    offset += 1
                room = max(0, height - preview_y - offset - 1)
                for index, runs in enumerate(preview_body[-room:] if room else []):
                    put_runs(screen, preview_y + offset + index, runs, renderer)
        footer = (message or f"{len(matches)} of {len(rows)} sessions   "
                  "Enter attach \u00b7 Ctrl-N new \u00b7 Ctrl-R rename \u00b7 "
                  "Ctrl-D delete \u00b7 F1 help")
        put(screen, height - 1, 0, footer, style.warning if message_alert else style.muted)
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ("\x1b", "\x03"):
            return None
        if key in ("\n", "\r", curses.KEY_ENTER) and selected:
            return selected
        if key in (curses.KEY_DOWN, "\t"):
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
            refreshed, preview_at = 0, 0
        elif key == "\x10":
            show_preview = not show_preview
            preview_at = 0
        elif key == "\x07":
            show_usage_panel(screen, style)
        elif key == "\x0f" and selected:
            viewer(screen, tmux, selected, style)
            refreshed, preview_at = 0, 0
        elif key == curses.KEY_F1:
            show_help(screen, style)
        elif key == "\x12" and selected:
            name = prompt(screen, "Rename (Ctrl-U clears)", selected["name"], style)
            if name is not None:
                try:
                    tmux.rename(selected_id, name)
                    message, message_alert, refreshed = "Session renamed", False, 0
                except Error as error:
                    message, message_alert = str(error), True
        elif key in ("\x04", curses.KEY_DC) and selected:
            try:
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
        elif key == "\x17" and selected:
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


def interactive(tmux, client=None, show_preview=True, notify=True):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Error(f"The picker requires a terminal. Use {APP} list for text output.")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    client = tmux.client(client)
    selected = curses.wrapper(picker, tmux, show_preview=show_preview, notify=notify)
    return tmux.connect(selected["id"], client) if selected else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def list_lines(tmux, enabled):
    for row in tmux.session_details(detect=True):
        agent = AGENTS.get(row.get("agent") or "")
        status = row_status(row)
        glyph = {"finished": GLYPH["finished"], "running": GLYPH["running"],
                 "blocked": GLYPH["blocked"], "idle": GLYPH["idle"]}.get(status, GLYPH["shell"])
        role = {"finished": "finished", "running": "attached",
                "blocked": "warning", "idle": "muted"}.get(status, "muted")
        name = f"{clean(row['name']):<28}"
        state = f"{'attached' if row['attached'] else 'detached':<8}"
        segments = [(f"{glyph} ", role), (name, "heading")]
        if agent and not agent.is_shell:
            segments.append((f"{agent.label:<8}", agent.accent))
        else:
            segments.append((f"{'-':<8}", "muted"))
        segments.append((f"{state}  ", "attached" if row["attached"] else "muted"))
        segments.append((f"{clean(row.get('process', '')):<10} ", "default"))
        segments.append((short_path(row.get("cwd", "")), "muted"))
        if status == "blocked" and row.get("prompt"):
            segments.append(("  needs input: " + row["prompt"][:100], "warning"))
        elif row["unread"]:
            segments.append(("  " + GLYPH["finished"], "finished"))
        print(paint_segments(segments, enabled))


def doctor(tmux):
    print(f"{APP} {VERSION}")
    print(f"tmux: {tmux.run('-V')}")
    print(f"Helper: {SELF}")
    print(f"Inside tmux: {'yes' if os.environ.get('TMUX') else 'no'}")
    print(f"Repositories: {ROOT}")
    print(f"Default agent: {DEFAULT_AGENT}")
    for agent in AGENTS.values():
        if agent.is_shell:
            continue
        found = shutil.which(agent.binary)
        print(f"  {agent.label:<9} {found or 'missing'}")
    print(f"Notifications: {'on' if notify_default() else 'off'}"
          f"{'' if shutil.which('notify-send') else ' (notify-send not found)'}")
    print(f"Watch interval: {watch_interval()}s")
    print(f"Sessions: {len(tmux.sessions())}")
    print(f"Usage data: {'available' if codex_usage(0) else 'none found'}")


def watch_command(tmux, interval, once=False, notify_enabled=True):
    """Poll sessions and report when an agent starts waiting on the user."""
    previous = {}
    while True:
        try:
            rows = tmux.session_details(detect=True)
        except Error as error:
            print(f"{APP}: {clean(error)}", file=sys.stderr)
            rows = []
        for row in rows:
            status = row_status(row)
            if status == "blocked" and previous.get(row["id"]) != "blocked":
                agent = AGENTS.get(row.get("agent") or "")
                label = agent.label if agent and not agent.is_shell \
                    else (row.get("process") or "agent")
                prompt = row.get("prompt") or "waiting for input"
                print(f"{clean(row['name'])}: {clean(label)} needs input - {clean(prompt)}")
                send_blocked_notification(label, row["name"], prompt, notify_enabled)
            previous[row["id"]] = status
        if once:
            return 0
        time.sleep(interval)


def build_parser():
    parser = argparse.ArgumentParser(prog=APP, description=__doc__)
    parser.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    parser.add_argument("--no-preview", action="store_true",
                        help="start with pane previews hidden")
    parser.add_argument("--no-notify", action="store_true",
                        help="do not send a notification when an agent finishes")
    sub = parser.add_subparsers(dest="command")
    pick = sub.add_parser("pick", help="search and select sessions (default)")
    pick.add_argument("--client", help=argparse.SUPPRESS)
    pick.add_argument("--no-preview", action="store_true", default=argparse.SUPPRESS,
                      help="start with pane previews hidden")
    sub.add_parser("list", help="list running tmux sessions")
    sub.add_parser("ls", help="alias for list")
    sub.add_parser("names", help="print session names for shell completion")
    attach = sub.add_parser("attach", help="attach outside tmux; switch inside it")
    attach.add_argument("session")
    new = sub.add_parser("new", help="start an agent or shell in a named session")
    new.add_argument("name")
    new.add_argument("--agent", default=DEFAULT_AGENT,
                     help=f"agent to run (default: {DEFAULT_AGENT}); 'shell' for a plain session")
    new.add_argument("--cwd", default=str(Path.cwd()))
    new.add_argument("--shell", action="store_true", help="alias for --agent shell")
    new.add_argument("--detach", action="store_true", help="create without attaching")
    new.add_argument("--no-notify", action="store_true", help="no completion notification")
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
    sub.add_parser("usage", help="show locally available provider usage limits")
    sub.add_parser("agents", help="list known agents and whether they are installed")
    watch = sub.add_parser("watch", help="notify when an agent needs input")
    watch.add_argument("--interval", type=float, default=watch_interval(),
                       help="seconds between checks (default: %(default)s)")
    watch.add_argument("--once", action="store_true", help="check once and exit")
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
    command_position = 1 if args_list[:1] == ["--no-preview"] else 0
    if args_list[command_position:command_position + 1] == ["new"] and "--" in args_list:
        divider = args_list.index("--")
        agent_args, args_list = args_list[divider + 1:], args_list[:divider]
    args = parser.parse_args(args_list)
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
    if args.command in (None, "pick"):
        return interactive(tmux, getattr(args, "client", None),
                           show_preview=not args.no_preview,
                           notify=not args.no_notify)
    if args.command in ("list", "ls"):
        list_lines(tmux, color_enabled())
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
        sid = tmux.create(args.name, args.cwd, args.agent, agent_args,
                          notify=not args.no_notify)
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
                             notify_enabled=not args.no_notify)
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
