"""Offline fullscreen chat fixture for preview-follow tests; never calls an API."""

import os
from pathlib import Path
import sys
import termios
import tty


record = Path(sys.argv[1])
record.write_bytes(b"")
draft = "DRAFT_STAYS_UNSUBMITTED"
original = termios.tcgetattr(sys.stdin.fileno())


def render(latest=False):
    content = "LIVE_CHAT_MARKER" if latest else "OLD_CHAT_HISTORY"
    control = "" if latest else "New activity · ↓ Back to bottom · esc"
    sys.stdout.write("\033[2J\033[H" + content + "\033[18;1H" + control
                     + "\033[20;1H› " + draft + "\033[22;1H? for shortcuts")
    sys.stdout.flush()


try:
    tty.setraw(sys.stdin.fileno())
    sys.stdout.write("\033[?1049h")
    render()
    received = b""
    while True:
        chunk = os.read(sys.stdin.fileno(), 1024)
        if not chunk:
            break
        received += chunk
        record.write_bytes(received)
        if received.endswith(b"\033[1;5F"):
            render(latest=True)
        elif received.endswith(b"\033[5~"):
            render(latest=False)
finally:
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original)
