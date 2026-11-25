#!/usr/bin/env python3
"""Optional Discord notification command for agmux."""

import getpass
from http.client import HTTPSConnection, HTTPException
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time


WEBHOOK = re.compile(r"https://discord\.com(/api/(?:v[0-9]+/)?webhooks/[0-9]+/[A-Za-z0-9_-]+)")
SUPPRESS_EMBEDS = 1 << 2
SUPPRESS_NOTIFICATIONS = 1 << 12


def webhook_file():
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "agmux" / "discord-webhook"


def webhook_path(url):
    match = WEBHOOK.fullmatch(url.strip())
    if not match:
        raise ValueError("Expected an incoming webhook URL copied from discord.com.")
    return match[1]


def save_webhook(url):
    webhook_path(url)
    path = webhook_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".discord-webhook-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(url.strip() + "\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def load_webhook():
    with os.fdopen(os.open(webhook_file(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK),
                   encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Webhook file must be a private file owned by you (chmod 600).")
        return webhook_path(handle.read(2048))


def message(title, session, exit_code, quiet_completions=False):
    if session and exit_code and not title.endswith("needs input"):
        title = f"{title} (exit {exit_code})"
    content = f"{title} · {session}" if session else title
    flags = SUPPRESS_EMBEDS
    if exit_code == 0 and (quiet_completions or title.endswith(" closed")) and not title.endswith("needs input"):
        flags |= SUPPRESS_NOTIFICATIONS
    return {"content": WEBHOOK.sub("[webhook]", content)[:2000],
            "allowed_mentions": {"parse": []}, "flags": flags}


def deliver(path, payload):
    # wait=true asks Discord to acknowledge creation of the message. Never
    # follow a redirect or include the request path in diagnostics.
    for attempt in range(2):
        conn = HTTPSConnection("discord.com", timeout=6)
        try:
            conn.request("POST", path + "?wait=true", json.dumps(payload).encode(),
                         {"Content-Type": "application/json", "User-Agent": "agmux-discord/1"})
            response = conn.getresponse()
            if 200 <= response.status < 300:
                return
            if response.status != 429 or attempt:
                raise ValueError(f"Discord returned HTTP {response.status}.")
            try:
                delay = float(json.loads(response.read(4096))["retry_after"])
            except (ValueError, TypeError, KeyError):
                delay = -1
            if not math.isfinite(delay) or not 0 <= delay <= 30:
                raise ValueError("Discord rate-limited this notification.")
        finally:
            conn.close()
        time.sleep(delay)


def run(args, parser):
    if args.setup or args.test:
        if args.title is not None:
            parser.error("setup and test take no positional arguments")
    elif args.session is None:
        parser.error("supply an event title and session name, or use --setup or --test")
    try:
        if args.setup:
            if not sys.stdin.isatty():
                parser.error("--setup requires an interactive terminal")
            save_webhook(getpass.getpass("Discord webhook URL (hidden): "))
            print("Webhook saved. Set the following in your agmux config file:")
            print('notify_cmd = agmux discord -- "{title}" "{session}" "{exit_code}"')
        else:
            payload = (message("agmux Discord alerts connected", "", -1) if args.test else
                       message(args.title, args.session, args.exit_code, args.quiet_completions))
            deliver(load_webhook(), payload)
            if args.test:
                print("Discord accepted the direct webhook test. Run agmux doctor to check agent notification routing.")
    except (OSError, HTTPException, UnicodeError, EOFError):
        print("agmux-discord: could not read the private webhook file or reach Discord.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"agmux-discord: {error}", file=sys.stderr)
        return 1
    return 0
