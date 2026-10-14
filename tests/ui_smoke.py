"""Interactive (pseudo-terminal) smoke test for the agmux picker.

Runs against a private tmux socket with a fake agent binary, so it never
touches real sessions. Usage: ``python3 tests/ui_smoke.py``.
"""

import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time


MODULE = Path(__file__).resolve().parents[1] / "agmux.py"
TMUX_CONF = MODULE.parent / "tmux.conf"


with tempfile.TemporaryDirectory(prefix='agmux-ui-') as directory:
    socket = str(Path(directory) / 'tmux.sock')
    root = Path(directory)
    repositories = root / 'repos'
    project = repositories / 'demo-project'
    project.mkdir(parents=True)
    codexhome = root / 'codexhome'  # empty: no usage data in the header
    codexhome.mkdir()
    fakebin = root / 'bin'
    fakebin.mkdir()
    fakecodex = fakebin / 'codex'
    record = root / 'codex-started'
    fakecodex.write_text('#!/usr/bin/python3\nfrom pathlib import Path\nPath(' + repr(str(record)) + ').write_text("started")\n')
    fakecodex.chmod(0o755)
    # A launcher so the popup binding (which calls `agmux` on PATH) resolves.
    launcher = fakebin / 'agmux'
    launcher.write_text('#!/bin/bash\nexec ' + sys.executable + ' ' + str(MODULE) + ' "$@"\n')
    launcher.chmod(0o755)
    env = dict(os.environ, AGMUX_TMUX_SOCKET=socket, AGMUX_REPOS=str(repositories),
               CODEX_HOME=str(codexhome), OPENCODE_DATA_DIR=str(root / 'ocdata'),
               AGMUX_NOTIFY='0', AGMUX_ASCII='1', AGMUX_CONFIG=os.devnull,
               TERM='xterm-256color', PATH=str(fakebin) + os.pathsep + os.environ['PATH'])
    env.pop('TMUX', None)
    env.pop('TMUX_PANE', None)
    command = ['tmux', '-S', socket]
    def tmux(*args):
        return subprocess.check_output(command + list(args), env=env, text=True).strip()
    processes = []
    masters = []
    ttys = {}
    def launch(args):
        master, slave = pty.openpty()
        ttys[master] = os.ttyname(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
        proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env,
                                cwd=directory, start_new_session=True)
        os.close(slave)
        processes.append(proc)
        masters.append(master)
        return master, proc
    def wait_output(master, needle, timeout=5):
        output = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
                if needle in output:
                    return output
        raise AssertionError(f'Did not find {needle!r}; output tail={output[-1000:]!r}')
    def wait_clients(expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if tmux('list-clients', '-F', '#{session_name}') == expected:
                return
            time.sleep(0.05)
        raise AssertionError(f'Client did not switch to {expected}')
    def wait_client(master, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            clients = dict(line.split('|', 1) for line in
                           tmux('list-clients', '-F', '#{client_tty}|#{session_name}').splitlines())
            if clients.get(ttys[master]) == expected:
                return
            time.sleep(0.05)
        raise AssertionError(f'Client {ttys[master]} did not switch to {expected}')
    try:
        tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'alpha', '-c', directory)
        tmux('new-session', '-d', '-s', 'beta', '-c', directory)
        tmux('source-file', str(TMUX_CONF))
        master, proc = launch([sys.executable, str(MODULE)])
        wait_output(master, b'agmux 0.')
        os.write(master, b'beta')
        time.sleep(0.3)
        os.write(master, b'\x12')
        wait_output(master, b'Rename (Ctrl-U clears)')
        os.write(master, b'\x15beta renamed\r')
        wait_output(master, b'Session renamed')
        os.write(master, b'\r')
        wait_clients('beta renamed')
        os.write(master, b'\x02d')
        proc.wait(timeout=5)
        assert proc.returncode == 0
        print('PASS: interactive filtering, rename, attach, and detach')

        original = tmux('capture-pane', '-p', '-t', '=alpha:')
        master, proc = launch([sys.executable, str(MODULE), 'peek', 'alpha'])
        wait_output(master, b'Read-only output: alpha')
        os.write(master, b'printf CX_SHOULD_NOT_RUN\r')
        time.sleep(0.1)
        assert tmux('list-clients', '-F', '#{session_name}') == ''
        assert tmux('capture-pane', '-p', '-t', '=alpha:') == original
        os.write(master, b'\x1b')
        proc.wait(timeout=5)
        assert proc.returncode == 0
        print('PASS: read-only viewer attaches no client and forwards no input')

        master, proc = launch(command + ['attach-session', '-t', '=alpha'])
        wait_clients('alpha')
        other_master, other_proc = launch(command + ['attach-session', '-t', '=alpha'])
        wait_client(other_master, 'alpha')
        pane = tmux('list-panes', '-t', '=alpha:', '-F', '#{pane_id}')
        inside = dict(env, TMUX=f'{socket},0,0', TMUX_PANE=pane)
        denied = subprocess.run([sys.executable, str(MODULE), 'attach', 'beta renamed'],
                                env=inside, text=True, capture_output=True)
        assert denied.returncode == 1 and 'Multiple' in denied.stderr
        wait_client(master, 'alpha')
        wait_client(other_master, 'alpha')
        os.write(master, b'\x02\x13')
        wait_output(master, b'agmux 0.')
        os.write(master, b'beta')
        time.sleep(0.3)
        os.write(master, b'\r')
        wait_client(master, 'beta renamed')
        wait_client(other_master, 'alpha')
        pane = tmux('list-panes', '-t', '=beta renamed:', '-F', '#{pane_id}')
        inside = dict(env, TMUX=f'{socket},0,0', TMUX_PANE=pane)
        subprocess.run([sys.executable, str(MODULE), 'last'], env=inside, check=True)
        wait_client(master, 'alpha')
        wait_client(other_master, 'alpha')
        os.write(master, b'\x02d')
        proc.wait(timeout=5)
        assert proc.returncode == 0
        os.write(other_master, b'\x02d')
        other_proc.wait(timeout=5)
        assert other_proc.returncode == 0
        print('PASS: two-client isolation, popup switching, and last-session navigation')

        master, proc = launch([sys.executable, str(MODULE), '--no-preview'])
        wait_output(master, b'agmux 0.')
        os.write(master, b'\x1bOP')  # F1 in xterm application mode
        wait_output(master, b'keyboard help')
        os.write(master, b'\r')
        wait_output(master, b'Enter attach')
        os.write(master, b'\x07')  # Ctrl-G: usage panel
        wait_output(master, b'usage limits')
        os.write(master, b'\r')
        wait_output(master, b'Enter attach')
        os.write(master, b'\x0f')
        wait_output(master, b'Read-only output:')
        os.write(master, b'\x1b')
        wait_output(master, b'Enter attach')
        os.write(master, b'\x10')  # show preview
        wait_output(master, b'bash  |')
        os.write(master, b'\x1b')
        proc.wait(timeout=5)
        assert proc.returncode == 0
        print('PASS: help, usage panel, preview toggle, and in-picker read-only viewer')

        master, proc = launch([sys.executable, str(MODULE)])
        wait_output(master, b'agmux 0.')
        os.write(master, b'\x0e')
        wait_output(master, b'choose what to run')
        os.write(master, b'\r')
        wait_output(master, b'choose a directory')
        os.write(master, b'demo')
        time.sleep(0.3)
        os.write(master, b'\r')
        wait_output(master, b'Session name (Ctrl-U clears)')
        os.write(master, b'\r')
        wait_clients('codex-demo-project')
        deadline = time.monotonic() + 5
        while not record.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert record.exists(), 'Fake Codex was not launched'
        deadline = time.monotonic() + 5
        cwd = ''
        while cwd != str(project) and time.monotonic() < deadline:
            cwd = tmux('display-message', '-p', '-t', '=codex-demo-project:', '#{pane_current_path}')
            time.sleep(0.02)
        assert cwd == str(project), (cwd, str(project))
        os.write(master, b'\x02d')
        proc.wait(timeout=5)
        assert proc.returncode == 0
        print('PASS: new-session wizard, fake agent launch, and selected directory')

        tmux('new-session', '-d', '-s', 'victim', '-c', directory)
        tmux('new-session', '-d', '-s', 'bystander', '-c', directory)
        master, proc = launch([sys.executable, str(MODULE)])
        wait_output(master, b'agmux 0.')
        os.write(master, b'victim')
        time.sleep(0.3)
        os.write(master, b'\x04')  # Ctrl-D: open the deletion confirmation
        wait_output(master, b'Delete session - this cannot be undone')
        os.write(master, b'\x1b')  # Esc cancels, returning to the picker
        wait_output(master, b'Enter attach')
        assert 'victim' in tmux('list-sessions', '-F', '#{session_name}').split()
        os.write(master, b'\x17')  # Ctrl-W: detached shell window
        wait_output(master, b'Added shell window')
        windows = tmux('list-windows', '-t', '=victim:', '-F', '#{window_index}').split()
        assert windows == ['0', '1'], windows
        os.write(master, b'\x04')  # Ctrl-D again, then type the exact name
        wait_output(master, b'Delete session - this cannot be undone')
        os.write(master, b'victim\r')
        wait_output(master, b'Deleted session victim')
        sessions = tmux('list-sessions', '-F', '#{session_name}').split()
        assert 'victim' not in sessions and 'bystander' in sessions, sessions
        os.write(master, b'\x1b')
        proc.wait(timeout=5)
        assert proc.returncode == 0
        print('PASS: picker deletion confirmation/cancel and detached shell window')
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        subprocess.run(command + ['kill-server'], env=env, capture_output=True)
        for master in masters:
            os.close(master)
