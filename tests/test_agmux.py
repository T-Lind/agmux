import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "agmux.py"
# Never let a developer's real config file leak into the tests.
os.environ.setdefault("AGMUX_CONFIG", os.devnull)
spec = importlib.util.spec_from_file_location("agmux", MODULE)
agmux = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agmux)


class TmuxIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agmux-test-")
        self.root = Path(self.temp.name)
        self.socket = str(self.root / "tmux.sock")
        self.record = self.root / "argv.json"
        self.bin = self.root / "bin with spaces"
        self.bin.mkdir()
        fake = self.bin / "codex"
        fake.write_text("#!/usr/bin/python3\nimport json, os, sys\n"
                        "from pathlib import Path\n"
                        "Path(os.environ['AGMUX_TEST_ARGV']).write_text(json.dumps(sys.argv[1:]))\n")
        fake.chmod(0o755)
        env = dict(os.environ, AGMUX_TMUX_SOCKET=self.socket, AGMUX_TEST_ARGV=str(self.record),
                   PATH=str(self.bin) + os.pathsep + os.environ["PATH"], SHELL="/bin/bash")
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        subprocess.run(["tmux", "-S", self.socket, "-f", "/dev/null", "new-session",
                        "-d", "-s", "seed", "-c", str(self.root)], check=True)
        self.tmux = agmux.Tmux()

    def tearDown(self):
        subprocess.run(["tmux", "-S", self.socket, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.environment.stop()
        self.temp.cleanup()

    def test_exact_resolution_and_rename(self):
        sid = self.tmux.create("work-long", self.root, agent="shell")
        other = self.tmux.create("work", self.root, agent="shell")
        self.assertEqual(self.tmux.resolve("work")["id"], other)
        with self.assertRaises(agmux.Error):
            self.tmux.resolve("wor")
        self.tmux.rename(sid, "review with spaces")
        self.assertEqual(self.tmux.resolve("review with spaces")["id"], sid)
        with self.assertRaises(agmux.Error):
            self.tmux.create("work", self.root, agent="shell")

    def test_names_are_data_and_cwd_is_preserved(self):
        directory = self.root / "project with spaces"
        directory.mkdir()
        name = "-odd $(touch PWNED) ' quote"
        sid = self.tmux.create(name, directory, agent="shell")
        self.assertEqual(self.tmux.resolve(name)["id"], sid)
        cwd = self.tmux.run("display-message", "-p", "-t", sid, "#{pane_current_path}")
        self.assertEqual(cwd, str(directory))
        self.assertFalse((directory / "PWNED").exists())

    def test_agent_arguments_and_shell_after_exit(self):
        arguments = ["--model", "name with spaces", "$(touch PWNED)", "it's literal", "line\nbreak",
                     ";", "kill-server", "#{session_name}", "`touch PWNED`", "--"]
        sid = self.tmux.create("codex-argv", self.root, agent="codex", agent_args=arguments)
        for _ in range(100):
            if self.record.exists():
                break
            time.sleep(0.02)
        self.assertTrue(self.record.exists(), "fake agent did not launch")
        self.assertEqual(json.loads(self.record.read_text()), arguments)
        self.assertFalse((self.root / "PWNED").exists())
        self.assertEqual(self.tmux.resolve(sid)["name"], "codex-argv")
        # The pane must survive the short-lived agent command.
        for _ in range(100):
            command = self.tmux.run("display-message", "-p", "-t", sid, "#{pane_current_command}")
            if command == "bash":
                break
            time.sleep(0.02)
        self.assertEqual(command, "bash")

    def test_create_records_agent_and_read_clears_unread(self):
        sid = self.tmux.create("tracked", self.root, agent="shell")
        row = self.tmux.resolve("tracked")
        self.assertEqual(row["agent"], "shell")
        self.assertEqual(row["status"], "shell")
        self.tmux.session_option(sid, "agmux_unread", "1")
        self.assertEqual(self.tmux.resolve(sid)["unread"], 1)
        self.assertEqual(agmux.row_status(self.tmux.resolve(sid)), "finished")
        self.tmux.mark_read(sid)
        self.assertEqual(self.tmux.resolve(sid)["unread"], 0)

    def test_read_clears_finished_badge_for_an_agent_session(self):
        sid = self.tmux.create("agent-done", self.root, agent="shell")
        self.tmux.session_option(sid, "agmux_agent", "codex")
        self.tmux.session_option(sid, "agmux_status", "finished")
        self.tmux.session_option(sid, "agmux_unread", "1")
        self.assertEqual(agmux.row_status(self.tmux.resolve(sid)), "finished")
        self.tmux.mark_read(sid)
        row = self.tmux.resolve(sid)
        self.assertEqual(row["unread"], 0)
        self.assertEqual(row["status"], "idle")
        # The agent exited and a login shell is in the pane, so it is idle,
        # not "running" forever just because the agent label is retained.
        self.assertEqual(agmux.row_status(row), "idle")
        # Attaching to a live agent must not clobber its running state.
        live = self.tmux.create("agent-live", self.root, agent="shell")
        self.tmux.session_option(live, "agmux_agent", "codex")
        self.tmux.session_option(live, "agmux_status", "running")
        self.tmux.mark_read(live)
        self.assertEqual(self.tmux.resolve(live)["status"], "running")

    def test_pane_lines_are_pinned_to_newest_output_when_scrolled(self):
        sid = self.tmux.create("state-scrolled", self.root, agent="shell")
        self.tmux.run("respawn-pane", "-k", "-t", f"{sid}:", "/bin/bash", "-c",
                      "printf 'old-%03d\\n' $(seq 1 200); echo STATE_NEWEST; sleep 30")
        pane = self.tmux.run("list-panes", "-s", "-t", sid, "-F", "#{pane_id}")
        for _ in range(100):
            if "STATE_NEWEST" in "\n".join(self.tmux.pane_lines(pane, 60)):
                break
            time.sleep(0.02)
        self.tmux.run("copy-mode", "-t", pane)
        for _ in range(25):
            self.tmux.run("send-keys", "-t", pane, "-X", "scroll-up")
        self.assertIn("STATE_NEWEST", "\n".join(self.tmux.pane_lines(pane, 40)))

    def test_tracked_agent_pane_reports_state_after_switching_windows(self):
        sid = self.tmux.create("agent-window", self.root, agent="shell")
        agent_pane = self.tmux.run("list-panes", "-s", "-t", sid, "-F", "#{pane_id}")
        self.tmux.run("respawn-pane", "-k", "-t", agent_pane, "/bin/bash", "-c",
                      "printf 'Allow command?\\n'; sleep 30")
        self.tmux.session_option(sid, "agmux_agent", "codex")
        self.tmux.session_option(sid, "agmux_pane", agent_pane)
        self.tmux.run("new-window", "-t", sid, "-c", str(self.root), "/bin/sleep", "30")
        self.tmux.run("select-window", "-t", f"{sid}:1")
        row = None
        for _ in range(100):
            row = next(r for r in self.tmux.session_details(detect=True) if r["id"] == sid)
            if row["state"] == "blocked":
                break
            time.sleep(0.02)
        self.assertEqual(row["state"], "blocked")
        self.assertIn("Allow command?", row["prompt"])
        # The preview still follows the active (shell) window.
        self.assertEqual(row["process"], "sleep")

    def test_cancel_modes_clears_copy_mode(self):
        sid = self.tmux.create("moded", self.root, agent="shell")
        pane = self.tmux.run("list-panes", "-s", "-t", sid, "-F", "#{pane_id}")
        self.tmux.run("copy-mode", "-t", pane)
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", pane, "#{pane_in_mode}"), "1")
        self.tmux.cancel_modes(sid)
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", pane, "#{pane_in_mode}"), "0")
        # A session with no active modes is left alone without error.
        self.tmux.cancel_modes(sid)

    def test_detects_agent_from_foreground_command(self):
        bin_dir = self.root / "openbin"
        bin_dir.mkdir()
        fake_opencode = bin_dir / "opencode"
        fake_opencode.symlink_to("/bin/sleep")
        sid = self.tmux.create("detected", self.root, agent="shell")
        self.tmux.run("set-option", "-t", sid, "-u", "@agmux_agent")  # clear tracked agent
        self.tmux.run("respawn-pane", "-k", "-t", f"{sid}:", str(fake_opencode), "30")
        for _ in range(100):
            row = next((r for r in self.tmux.session_details() if r["id"] == sid), None)
            if row and row["agent"] == "opencode":
                break
            time.sleep(0.02)
        self.assertIsNotNone(row)
        self.assertEqual(row["agent"], "opencode")
        # An unknown foreground command stays untracked rather than guessing.
        self.tmux.run("respawn-pane", "-k", "-t", f"{sid}:", "/bin/sleep", "30")
        for _ in range(100):
            row = next((r for r in self.tmux.session_details() if r["id"] == sid), None)
            if row and row["process"] == "sleep":
                break
            time.sleep(0.02)
        self.assertEqual(row["agent"], "")

    def test_searchable_metadata_uses_active_pane(self):
        directory = self.root / "unique-project"
        directory.mkdir()
        sid = self.tmux.create("unrelated-name", directory, agent="shell")
        self.tmux.run("new-window", "-t", sid, "-c", str(directory), "-n", "active",
                      "/bin/sleep", "30")
        for _ in range(100):
            row = next(r for r in self.tmux.session_details() if r["id"] == sid)
            if row["process"] == "sleep":
                break
            time.sleep(0.02)
        self.assertEqual(row["process"], "sleep")
        self.assertEqual(row["cwd"], str(directory))
        self.assertTrue(agmux.fuzzy("unique-project", agmux.searchable(row)))
        self.assertTrue(agmux.fuzzy("sleep", agmux.searchable(row)))

    def test_literal_name_wins_over_session_id(self):
        seed_id = self.tmux.resolve("seed")["id"]
        named_id = self.tmux.create(seed_id, self.root, agent="shell")
        self.assertEqual(self.tmux.resolve(seed_id)["id"], named_id)

    def test_tmux_conf_installs_prefixless_bindings(self):
        self.tmux.run("source-file", str(MODULE.parent / "tmux.conf"))
        keys = self.tmux.run("list-keys", "-T", "root").splitlines()
        picker = [line for line in keys if " M-s " in line]
        killer = [line for line in keys if " M-x " in line]
        self.assertTrue(any("detach-client" in line and "socket_path" in line
                            for line in picker), picker)
        self.assertTrue(any("confirm-before" in line and "kill-session" in line
                            for line in killer), killer)

    def test_socket_permission_errors_are_reported(self):
        denied = subprocess.CompletedProcess([], 1, "", "error connecting to /private/tmux (Permission denied)")
        with patch.object(agmux.subprocess, "run", return_value=denied):
            with self.assertRaisesRegex(agmux.Error, "Permission denied"):
                self.tmux.sessions()

    def test_tmux_timeout_is_reported(self):
        with patch.object(agmux.subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 5)):
            with self.assertRaisesRegex(agmux.Error, "five seconds"):
                self.tmux.sessions()

    def test_preview_follows_active_window_and_pane(self):
        sid = self.tmux.create("preview", self.root, agent="shell")
        self.tmux.run("new-window", "-t", sid, "-c", str(self.root), "-n", "active", "/bin/bash", "-c",
                      "printf 'VISIBLE_MARKER\\n'; sleep 30")
        for _ in range(100):
            preview = self.tmux.preview(sid)
            if any("VISIBLE_MARKER" in line for line in preview):
                break
            time.sleep(0.02)
        self.assertIn("VISIBLE_MARKER", "\n".join(preview))
        self.assertIn(str(self.root), preview[0])

    def test_cli_detached_and_completion_with_spaces(self):
        self.assertEqual(agmux.main(["new", "cli with spaces", "--cwd", str(self.root),
                                     "--shell", "--detach"]), 0)
        script = 'source "$1"; COMP_WORDS=(agmux attach "cli"); COMP_CWORD=2; _agmux_complete; declare -p COMPREPLY'
        env = dict(os.environ, PATH=str(MODULE.parent) + os.pathsep + os.environ["PATH"])
        # Use a temporary launcher to exercise the shipped completion against real tmux.
        launcher = self.bin / "agmux"
        launcher.write_text('#!/bin/bash\nexec /usr/bin/python3 ' + agmux.shlex.quote(str(MODULE)) + ' "$@"\n')
        launcher.chmod(0o755)
        result = subprocess.run(["bash", "-c", script, "bash", str(MODULE.parent / "completion.bash")],
                                text=True, capture_output=True, env=env, check=True)
        self.assertIn('[0]="cli with spaces"', result.stdout)

    def test_cli_new_forwards_agent_args_after_divider(self):
        self.assertEqual(agmux.main(["new", "cli-args", "--cwd", str(self.root), "--detach",
                                     "--", "--model", "x y", "$(touch PWNED)", ";"]), 0)
        for _ in range(100):
            if self.record.exists():
                break
            time.sleep(0.02)
        self.assertTrue(self.record.exists())
        self.assertEqual(json.loads(self.record.read_text()),
                         ["--model", "x y", "$(touch PWNED)", ";"])
        self.assertFalse((self.root / "PWNED").exists())

    def test_empty_server_and_invalid_inputs(self):
        with self.assertRaises(agmux.Error):
            self.tmux.create("bad:name", self.root, agent="shell")
        with self.assertRaises(agmux.Error):
            self.tmux.create("valid", self.root / "missing", agent="shell")
        with self.assertRaises(agmux.Error):
            self.tmux.create("valid", self.root, agent="mystery")
        self.tmux.run("kill-session", "-t", self.tmux.resolve("seed")["id"])
        self.assertEqual(self.tmux.sessions(), [])

    def test_confirm_delete_answer_is_exact(self):
        self.assertFalse(agmux.confirm_delete_answer("work", ""))
        self.assertTrue(agmux.confirm_delete_answer("work", "work"))
        self.assertTrue(agmux.confirm_delete_answer("work", "work\n"))  # trailing newline is fine
        self.assertTrue(agmux.confirm_delete_answer("work", "work\r\n"))
        self.assertFalse(agmux.confirm_delete_answer("work", "work-long"))
        self.assertFalse(agmux.confirm_delete_answer("work", "wor"))
        self.assertFalse(agmux.confirm_delete_answer("work", "work extra"))

    def test_delete_exact_target_leaves_others_intact(self):
        victim = self.tmux.create("victim", self.root, agent="shell")
        keeper = self.tmux.create("keeper", self.root, agent="shell")
        info = self.tmux.describe(victim)
        self.assertEqual(info["client_count"], 0)
        self.assertEqual(info["pane_count"], 1)
        self.tmux.delete(victim, expected=info)
        names = [row["name"] for row in self.tmux.sessions()]
        self.assertNotIn("victim", names)
        self.assertIn("keeper", names)
        self.assertEqual(self.tmux.resolve("keeper")["id"], keeper)

    def test_delete_refuses_changed_target(self):
        sid = self.tmux.create("victim", self.root, agent="shell")
        info = self.tmux.describe(sid)
        # A pane/window added while confirming must abort the delete.
        self.tmux.run("new-window", "-d", "-t", sid, "-c", str(self.root))
        with self.assertRaisesRegex(agmux.Error, "changed during confirmation"):
            self.tmux.delete(sid, expected=info)
        self.assertIn("victim", [row["name"] for row in self.tmux.sessions()])
        # A rename while confirming must abort it too.
        info = self.tmux.describe(sid)
        self.tmux.rename(sid, "victim-renamed")
        with self.assertRaisesRegex(agmux.Error, "changed during confirmation"):
            self.tmux.delete(sid, expected=info)
        self.assertIn("victim-renamed", [row["name"] for row in self.tmux.sessions()])
        # With a fresh snapshot the delete proceeds.
        self.tmux.delete(sid, expected=self.tmux.describe(sid))
        self.assertNotIn("victim-renamed", [row["name"] for row in self.tmux.sessions()])

    def test_delete_of_missing_session_reports(self):
        self.tmux.run("kill-session", "-t", self.tmux.resolve("seed")["id"])
        with self.assertRaises(agmux.Error):
            self.tmux.delete("$999")

    def test_shell_window_cwd_and_detached(self):
        directory = self.root / "project"
        directory.mkdir()
        sid = self.tmux.create("host", directory, agent="shell")
        index = self.tmux.shell_window(sid)
        self.assertEqual(index, "1")
        windows = {line.split(" ", 2)[0]: line.split(" ", 2)[1:]
                   for line in self.tmux.run(
                       "list-windows", "-t", sid, "-F",
                       "#{window_index} #{window_active} #{window_name}").splitlines()}
        self.assertEqual(windows["0"][0], "1")   # original window stays active
        self.assertEqual(windows["1"][0], "0")   # the new shell window is detached
        cwd = self.tmux.run("display-message", "-p", "-t", f"{sid}:1", "#{pane_current_path}")
        self.assertEqual(cwd, str(directory))
        other = self.root / "elsewhere"
        other.mkdir()
        index = self.tmux.shell_window(sid, cwd=str(other), name="scratch")
        self.assertEqual(index, "2")
        cwd = self.tmux.run("display-message", "-p", "-t", f"{sid}:2", "#{pane_current_path}")
        self.assertEqual(cwd, str(other))
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", f"{sid}:2",
                                       "#{window_name}"), "scratch")
        with self.assertRaises(agmux.Error):
            self.tmux.shell_window(sid, cwd=str(self.root / "missing"))

    def test_cli_delete_confirmation_and_plain_output(self):
        self.tmux.create("guarded", self.root, agent="shell")
        # A non-matching answer cancels and leaves the session alone.
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.object(agmux.sys, "stdin") as stdin:
                stdin.isatty.return_value = True
                stdin.readline.return_value = "not-the-name\n"
                self.assertEqual(agmux.main(["delete", "guarded"]), 0)
        self.assertIn("guarded", [row["name"] for row in self.tmux.sessions()])
        # The exact name deletes just that session.
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.object(agmux.sys, "stdin") as stdin:
                stdin.isatty.return_value = True
                stdin.readline.return_value = "guarded\n"
                self.assertEqual(agmux.main(["delete", "guarded"]), 0)
        self.assertNotIn("guarded", [row["name"] for row in self.tmux.sessions()])

    def test_cli_delete_without_terminal_requires_yes(self):
        self.tmux.create("guarded", self.root, agent="shell")
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.object(agmux.sys, "stdin") as stdin:
                stdin.isatty.return_value = False
                with self.assertRaises(agmux.Error):
                    agmux.main(["delete", "guarded"])
        self.assertIn("guarded", [row["name"] for row in self.tmux.sessions()])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agmux.main(["delete", "guarded", "--yes"]), 0)
        self.assertNotIn("guarded", [row["name"] for row in self.tmux.sessions()])

    def test_cli_shell_and_plain_piped_output(self):
        directory = self.root / "host dir"
        directory.mkdir()
        sid = self.tmux.create("host", directory, agent="shell")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agmux.main(["shell", "host", "--name", "extra"]), 0)
        self.assertIn("Added shell window 1", output.getvalue())
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", f"{sid}:1",
                                       "#{pane_current_path}"), str(directory))
        # Piped list output must stay free of our ANSI styling.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            agmux.main(["list"])
        self.assertIn("host", output.getvalue())
        self.assertNotIn("\033[", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            agmux.main(["names"])
        self.assertNotIn("\033[", output.getvalue())

    def test_color_helpers_respect_environment(self):
        class FakeTty:
            def isatty(self):
                return True

        self.assertFalse(agmux.color_enabled(io.StringIO()))
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(agmux.color_enabled(FakeTty()))
        env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "TERM")}
        env["TERM"] = "dumb"
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(agmux.color_enabled(FakeTty()))
        env["TERM"] = "xterm-256color"
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(agmux.color_enabled(FakeTty()))
        self.assertEqual(agmux.paint("name", "heading", False), "name")
        self.assertTrue(agmux.paint("name", "heading", True).startswith("\033[36m"))
        # Untrusted controls are neutralized before our own styling is applied.
        styled = agmux.paint("bad\x1b[31mname", "warning", True)
        self.assertEqual(styled.count("\x1b"), 2)

    def test_describe_reports_panes_and_clients(self):
        directory = self.root / "described"
        directory.mkdir()
        sid = self.tmux.create("described", directory, agent="shell")
        self.tmux.run("new-window", "-d", "-t", sid, "-c", str(directory), "-n", "second")
        info = self.tmux.describe(sid)
        self.assertEqual(info["name"], "described")
        self.assertEqual(info["windows"], 2)
        self.assertEqual(info["pane_count"], 2)
        self.assertEqual(info["client_count"], 0)
        self.assertTrue(any(pane["path"] == str(directory) for pane in info["panes"]))
        self.assertEqual(sum(1 for pane in info["panes"] if pane["active"]), 1)

    def test_session_resources_report_cpu_and_memory(self):
        sid = self.tmux.create("busy", self.root, agent="shell")
        self.tmux.run("respawn-pane", "-k", "-t", f"{sid}:", "/bin/sleep", "30")
        usage = {}
        for _ in range(100):
            agmux._PS_CACHE["at"] = 0.0   # do not reuse a sample from a prior test
            usage = self.tmux.session_resources()
            if usage.get(sid, (0.0, 0))[1] > 0:
                break
            time.sleep(0.02)
        self.assertIn(sid, usage)
        cpu, rss = usage[sid]
        self.assertGreaterEqual(cpu, 0.0)
        self.assertGreater(rss, 0)   # any live process has resident memory
        agmux._PS_CACHE["at"] = 0.0
        row = next(r for r in self.tmux.session_details(resources=True) if r["id"] == sid)
        self.assertGreater(row["rss"], 0)
        # Without the flag there is no sampling and the fields stay zeroed.
        row = next(r for r in self.tmux.session_details() if r["id"] == sid)
        self.assertEqual((row["cpu"], row["rss"]), (0.0, 0))

    def test_preview_ends_at_the_newest_output_when_scrolled_up(self):
        sid = self.tmux.create("scrolly", self.root, agent="shell")
        self.tmux.run("respawn-pane", "-k", "-t", f"{sid}:", "/bin/bash", "-c",
                      "printf 'line-%03d\\n' $(seq 1 200); echo NEWEST_MARKER; sleep 30")
        pane = self.tmux.run("list-panes", "-s", "-t", sid, "-F", "#{pane_id}")
        for _ in range(100):
            if "NEWEST_MARKER" in "\n".join(self.tmux.pane_lines(pane, 60)):
                break
            time.sleep(0.02)
        # Scroll the pane far up into copy mode, as a user might while reading.
        self.tmux.run("copy-mode", "-t", pane)
        for _ in range(25):
            self.tmux.run("send-keys", "-t", pane, "-X", "scroll-up")
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", pane,
                                       "#{pane_in_mode}"), "1")
        preview = self.tmux.preview(sid, 10)
        self.assertIn("NEWEST_MARKER", "\n".join(preview))
        self.assertNotIn("line-001", "\n".join(preview))

    def test_list_json_records_cpu_memory_and_state(self):
        self.tmux.create("json-row", self.root, agent="shell")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agmux.main(["list", "--json"]), 0)
        records = json.loads(output.getvalue())
        row = next(record for record in records if record["name"] == "json-row")
        self.assertEqual(row["status"], "shell")
        self.assertIn("rss", row)
        self.assertIn("cpu", row)
        self.assertEqual(row["agent"], "shell")
        self.assertIn("clients", row)
        self.assertIn("unread", row)

    def test_list_can_skip_resources(self):
        self.tmux.create("no-resources", self.root, agent="shell")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agmux.main(["list", "--json", "--no-resources"]), 0)
        row = next(record for record in json.loads(output.getvalue())
                   if record["name"] == "no-resources")
        self.assertEqual((row["cpu"], row["rss"]), (0.0, 0))

    def test_info_command_json_and_text(self):
        sid = self.tmux.create("info-target", self.root, agent="shell")
        self.tmux.run("new-window", "-d", "-t", sid, "-c", str(self.root), "-n", "second")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agmux.main(["info", "info-target", "--json"]), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["name"], "info-target")
        self.assertEqual(record["status"], "shell")
        self.assertEqual(record["pane_count"], 2)
        self.assertEqual(len(record["panes"]), 2)
        self.assertIn("client_ttys", record)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agmux.main(["info", "info-target"]), 0)
        text = output.getvalue()
        self.assertIn("info-target", text)
        self.assertIn("windows: 2", text)
        self.assertIn("window 0", text)

    def test_usage_json_is_machine_readable(self):
        with patch.object(agmux, "codex_usage",
                          return_value={"plan_type": "plus", "primary": {"used_percent": 5.0}}), \
                patch.object(agmux, "opencode_usage", return_value=None):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(agmux.main(["usage", "--json"]), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["codex"]["plan_type"], "plus")
        self.assertIsNone(data["opencode"])


class AgentMetadata(unittest.TestCase):
    def test_detect_agent(self):
        self.assertEqual(agmux.detect_agent("codex").key, "codex")
        self.assertEqual(agmux.detect_agent("/usr/bin/opencode").key, "opencode")
        self.assertIsNone(agmux.detect_agent("python3"))
        self.assertIsNone(agmux.detect_agent(""))
        self.assertEqual(agmux.detect_agent("python3", explicit="codex").key, "codex")

    def test_row_status(self):
        self.assertEqual(agmux.row_status({"unread": 1}), "finished")
        self.assertEqual(agmux.row_status({"status": "finished"}), "finished")
        self.assertEqual(agmux.row_status({"status": "running"}), "running")
        self.assertEqual(agmux.row_status({"agent": "codex"}), "running")
        self.assertEqual(agmux.row_status({"agent": "shell"}), "shell")
        self.assertEqual(agmux.row_status({}), "shell")
        # A tracked agent that exited and left a login shell is idle.
        self.assertEqual(agmux.row_status({"agent": "codex", "status": "idle"}), "idle")
        self.assertEqual(agmux.row_status({"agent": "codex", "process": "bash"}), "idle")
        self.assertEqual(agmux.row_status({"agent": "codex", "process": "node"}), "running")

    def test_short_path_home_and_humanize_tokens(self):
        self.assertEqual(agmux.short_path(str(Path.home())), "~")
        self.assertEqual(agmux.short_path(str(Path.home() / "work")), "~/work")
        self.assertEqual(agmux.humanize_tokens(999), "999")
        self.assertEqual(agmux.humanize_tokens(1587), "1.6K")
        self.assertEqual(agmux.humanize_tokens(12345), "12K")
        self.assertEqual(agmux.humanize_tokens(2_500_000), "2.5M")

    def test_default_name_per_agent(self):
        self.assertEqual(agmux.default_name("/x/demo", [], "codex"), "codex-demo")
        self.assertEqual(agmux.default_name("/x/demo", [], "opencode"), "opencode-demo")
        self.assertEqual(agmux.default_name("/x/demo", [], "shell"), "sh-demo")
        self.assertEqual(agmux.default_name("/x/demo", [{"name": "codex-demo"}], "codex"),
                         "codex-demo-2")

    def test_window_label_and_humanize(self):
        self.assertEqual(agmux.window_label(300), "5h")
        self.assertEqual(agmux.window_label(10080), "7d")
        self.assertEqual(agmux.humanize_delta(45), "45s")
        self.assertEqual(agmux.humanize_delta(90), "1m")
        self.assertEqual(agmux.humanize_delta(3600), "1h")
        self.assertEqual(agmux.humanize_delta(3660), "1h01m")


class AnsiAndState(unittest.TestCase):
    def test_parse_sgr_extracts_style_runs(self):
        runs = agmux.parse_sgr("\x1b[1;31mred bold\x1b[0m plain")
        self.assertEqual(runs[0][0], "red bold")
        self.assertEqual(runs[0][1][0], 1)   # red foreground
        self.assertEqual(runs[0][1][2], 1)   # bold
        self.assertEqual(runs[1][0], " plain")
        self.assertEqual(runs[1][1], agmux.DEFAULT_SGR)

    def test_parse_sgr_strips_non_sgr_controls(self):
        runs = agmux.parse_sgr("a\x1b[2Jb\x1b]0;title\x07c\x1b[38;2;255;0;0mred")
        text = "".join(part for part, _ in runs)
        self.assertNotIn("\x1b", text)
        self.assertIn("abc", text)
        self.assertEqual(runs[-1][1][0], agmux._rgb_to_xterm(255, 0, 0))

    def test_parse_sgr_handles_256_and_default_colors(self):
        runs = agmux.parse_sgr("\x1b[38;5;196mx\x1b[39m\x1b[48;5;17my")
        self.assertEqual(runs[0][1][0], 196)
        self.assertEqual(runs[1][1][0], -1)
        self.assertEqual(runs[1][1][1], 17)

    def test_render_ansi_plain_and_colored(self):
        line = "\x1b[31mred\x1b[0m"
        self.assertEqual(agmux.render_ansi([line], False), ["red"])
        colored = agmux.render_ansi([line], True)[0]
        self.assertIn("red", colored)
        # Only canonical SGR is re-emitted; the original bytes never pass through.
        self.assertNotIn("\x1b[31m", colored)
        self.assertIn("38;5;1", colored)

    def test_classify_screen(self):
        self.assertEqual(agmux.classify_screen(["\u2022 Working (3s \u2022 esc to interrupt)"])[0],
                         "working")
        self.assertEqual(agmux.classify_screen(["Allow command?", "> 1. Yes", "  2. No"])[0],
                         "blocked")
        self.assertEqual(agmux.classify_screen(["[y/N] proceed"])[0], "blocked")
        self.assertEqual(agmux.classify_screen(["Would you like to run this command?"])[0],
                         "blocked")
        self.assertEqual(agmux.classify_screen(["\u203a Ask Codex to do anything"])[0], "idle")
        state, prompt = agmux.classify_screen(["Do you want to allow this?"])
        self.assertEqual(state, "blocked")
        self.assertTrue(prompt)

    def test_classify_screen_prefers_live_work(self):
        # Real Codex line: reviewing an approval is work, not a user prompt.
        state, _ = agmux.classify_screen(
            ["\u2022 Reviewing approval request (10s \u2022 esc to interrupt)"])
        self.assertEqual(state, "working")
        # A genuine approval prompt with no live-work marker is blocked.
        state, prompt = agmux.classify_screen(
            ["Allow Codex to run this command?", "  1. Yes", "  2. No"])
        self.assertEqual(state, "blocked")
        self.assertTrue(prompt)

    def test_classify_screen_harness_prompts(self):
        for lines in (["Apply this change?"],
                      ["(Y)es/(N)o [Yes]:"],
                      ["Waiting for your approval"],
                      ["Do you want to proceed?", "\u276f 1. Yes", "  2. No"]):
            state, prompt = agmux.classify_screen(lines)
            self.assertEqual(state, "blocked", lines)
            self.assertTrue(prompt)
        for lines in (["\u23fa Searching... (esc to interrupt)"],
                      ["Running command (ctrl+c to stop)"]):
            self.assertEqual(agmux.classify_screen(lines)[0], "working", lines)

    def test_row_status_prefers_screen_state(self):
        self.assertEqual(agmux.row_status({"agent": "codex", "state": "blocked"}), "blocked")
        self.assertEqual(agmux.row_status({"agent": "codex", "state": "working"}), "running")
        self.assertEqual(agmux.row_status({"agent": "codex", "state": "idle"}), "idle")
        self.assertEqual(agmux.row_status({"agent": "shell", "state": "idle"}), "shell")
        self.assertEqual(agmux.row_status({"agent": "codex", "status": "finished"}), "finished")

    def test_state_ordering_puts_blocked_first(self):
        rows = [{"agent": "codex", "state": "idle"},
                {"agent": "codex", "state": "blocked"},
                {"unread": 1},
                {"agent": "codex", "state": "working"}]
        ordered = sorted(rows, key=agmux.state_priority)
        self.assertEqual(agmux.state_priority(ordered[0]), 0)
        self.assertEqual(agmux.row_status(ordered[0]), "blocked")

    def test_trim_chrome_codex(self):
        lines = ["assistant output here", "",
                 "\u2022 Working (3s \u2022 esc to interrupt)", "",
                 "\u203a Ask Codex to do anything", "",
                 "  GPT-6.1-Sol xhigh \u00b7 ~/source/repos/skyhook \u00b7 Review",
                 "  \u2190 for agents \u00b7 ? for shortcuts        \u26a0 4 warnings \u00b7 f2 to view"]
        trimmed = agmux.trim_chrome("codex", lines)
        joined = "\n".join(trimmed)
        self.assertIn("assistant output here", joined)
        self.assertIn("Working", joined)          # status line is kept
        self.assertNotIn("Ask Codex", joined)
        self.assertNotIn("for shortcuts", joined)  # footer is dropped
        self.assertNotIn("xhigh", joined)

    def test_trim_chrome_opencode(self):
        lines = ["tool output", "",
                 "  \u2503", "  \u2503",
                 "  \u2503  Build \u00b7 DeepSeek V4.1 Flash \u00b7 high",
                 "  \u2579\u2580\u2580\u2580\u2580\u2580",
                 "   \u2b1d\u25a0\u25a0\u25a0\u25a0\u2b1d esc interrupt   88.4K (9%) \u00b7 $0.02  ctrl+p commands"]
        trimmed = agmux.trim_chrome("opencode", lines)
        joined = "\n".join(trimmed)
        self.assertIn("tool output", joined)
        self.assertNotIn("esc interrupt", joined)
        self.assertNotIn("Build \u00b7", joined)

    def test_trim_chrome_claude_gemini_and_aider(self):
        claude = ["assistant output", "",
                  "  \u256d\u2500\u2500\u2500\u2500\u2500\u256e",
                  "  \u2502 >                         \u2502",
                  "  \u2570\u2500\u2500\u2500\u2500\u2500\u256f",
                  "  ? for shortcuts            \u23f5\u23f5 accept edits on"]
        trimmed = agmux.trim_chrome("claude", claude)
        self.assertEqual(trimmed, ["assistant output"])
        gemini = ["gemini output", "",
                  "  \u256d\u2500\u2500\u2500\u2500\u2500\u256e",
                  "  \u2502 > Type your message...    \u2502",
                  "  \u2570\u2500\u2500\u2500\u2500\u2500\u256f",
                  "  ? for shortcuts          context left: 90%"]
        self.assertEqual(agmux.trim_chrome("gemini", gemini), ["gemini output"])
        aider = ["aider output", "",
                 "Tokens: 1.2k sent, 3.4k received. Cost: $0.01 message, $0.02 session.",
                 "aider>"]
        self.assertEqual(agmux.trim_chrome("aider", aider), ["aider output"])
        # A live-work status line is content, not chrome.
        working = ["output", "  \u273b Thinking\u2026 (esc to interrupt)", ""]
        self.assertIn("Thinking", "\n".join(agmux.trim_chrome("claude", working)))

    def test_trim_chrome_leaves_shell_alone(self):
        lines = ["$ ls -la", "total 0", "$ echo hi", "", ""]
        self.assertEqual(agmux.trim_chrome("shell", lines), ["$ ls -la", "total 0", "$ echo hi"])
        self.assertEqual(agmux.trim_chrome(None, ["plain", "text"]), ["plain", "text"])

    def test_trim_chrome_handles_ansi_lines(self):
        # Regression: captured pane lines carry SGR escapes; the trimmer must
        # look at the plain text, not the raw bytes.
        esc, reset = "\x1b[38;2;1;2;3m", "\x1b[0m"
        codex = [esc + "real output" + reset,
                 esc + "  \u203a Ask Codex to do anything" + reset,
                 esc + "  GPT-6.1-Sol xhigh \u00b7 ~/x \u00b7 t" + reset,
                 esc + "  \u2190 for agents \u00b7 ? for shortcuts" + reset]
        self.assertEqual(len(agmux.trim_chrome("codex", codex)), 1)
        opencode = [esc + "tool output" + reset,
                    esc + "  \u2503" + reset,
                    esc + "  \u2503  Build \u00b7 X \u00b7 high" + reset,
                    esc + "  \u2579\u2580\u2580" + reset,
                    esc + "  \u2b1d\u25a0\u25a0\u2b1d esc interrupt  1K (9%) \u00b7 $0 ctrl+p commands" + reset]
        self.assertEqual(len(agmux.trim_chrome("opencode", opencode)), 1)

    def test_trim_chrome_can_be_disabled(self):
        lines = ["content", "  \u203a Ask Codex to do anything", ""]
        with patch.dict(os.environ, {"AGMUX_TRIM_CHROME": "0"}):
            self.assertEqual(agmux.trim_chrome("codex", lines),
                             ["content", "  \u203a Ask Codex to do anything"])

    def test_trim_chrome_keeps_unknown_footers(self):
        # A screen with no recognized strong footer is only blank-trimmed.
        lines = ["content", "\u2503", "\u2503", ""]
        self.assertEqual(agmux.trim_chrome("opencode", lines),
                         ["content", "\u2503", "\u2503"])


class Usage(unittest.TestCase):
    def test_rate_limit_parsing_and_report(self):
        with tempfile.TemporaryDirectory(prefix="agmux-usage-") as directory:
            home = Path(directory)
            day = home / "sessions" / "2026" / "01" / "01"
            day.mkdir(parents=True)
            rollout = day / "rollout-2026-01-01T00-00-00-abc.jsonl"
            record = {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": {
                    "plan_type": "plus",
                    "primary": {"used_percent": 42.0, "window_minutes": 300, "resets_at": time.time() + 3600},
                    "secondary": {"used_percent": 12.0, "window_minutes": 10080, "resets_at": time.time() + 86400},
                    "credits": {"has_credits": True, "balance": "3"},
                }},
            }
            rollout.write_text(json.dumps({"ignored": True}) + "\n" + json.dumps(record) + "\n")
            with patch.object(agmux, "CODEX_HOME", home), \
                    patch.object(agmux, "opencode_usage", return_value=None):
                limits = agmux.find_codex_rate_limits()
                self.assertEqual(limits["plan_type"], "plus")
                self.assertIn("Codex", agmux.usage_inline(limits))
                self.assertIn("42%", agmux.usage_inline(limits))
                report = "\n".join(text for text, _ in agmux.usage_report(limits=limits))
                self.assertIn("5h", report)
                self.assertIn("credits: 3", report)

    def test_opencode_usage_from_database(self):
        with tempfile.TemporaryDirectory(prefix="agmux-oc-") as directory:
            db = Path(directory) / "opencode.db"
            connection = sqlite3.connect(db)
            connection.execute("create table session_v2 (id text, title text, cost real, "
                               "tokens_input integer, tokens_output integer, model text, "
                               "time_updated integer)")
            connection.execute(
                "insert into session_v2 values ('s1','Build a thing',0.243,282628,77822,"
                "'{\"id\":\"deepseek-v4.1-flash\",\"providerID\":\"opencode-go\"}',200)")
            connection.execute(
                "insert into session_v2 values ('s2','Older',0.001,100,5,null,100)")
            connection.commit()
            connection.close()
            with patch.object(agmux, "OPENCODE_DB", db):
                agmux._OPENCODE_CACHE["at"] = 0.0
                usage = agmux.opencode_usage()
            agmux._OPENCODE_CACHE["at"] = 0.0
            self.assertEqual(usage["table"], "session_v2")
            self.assertEqual(usage["latest"]["title"], "Build a thing")
            self.assertEqual(usage["latest"]["tokens"], 282628 + 77822)
            self.assertEqual(usage["latest"]["model"], "opencode-go/deepseek-v4.1-flash")
            self.assertIn("OpenCode", agmux.opencode_inline(usage))
            self.assertIn("$0.24", agmux.opencode_inline(usage))
            report = "\n".join(text for text, _ in
                               agmux.usage_report(limits=None, opencode=usage))
            self.assertIn("Build a thing", report)
            self.assertIn("OpenCode", report)

    def test_opencode_usage_old_schema_and_extra_tokens(self):
        with tempfile.TemporaryDirectory(prefix="agmux-oc-old-") as directory:
            db = Path(directory) / "opencode.db"
            connection = sqlite3.connect(db)
            connection.execute("create table session (id text, title text, cost real, "
                               "tokens_input integer, tokens_output integer, model text, "
                               "time_updated integer)")
            connection.execute(
                "insert into session values ('s1','Old schema',0.5,1000,200,null,1700000000)")
            connection.commit()
            connection.close()
            with patch.object(agmux, "OPENCODE_DB", db):
                agmux._OPENCODE_CACHE["at"] = 0.0
                usage = agmux.opencode_usage()
            agmux._OPENCODE_CACHE["at"] = 0.0
            self.assertEqual(usage["table"], "session")
            self.assertEqual(usage["latest"]["tokens"], 1200)
            self.assertEqual(usage["latest"]["updated"], 1700000000)
            report = "\n".join(text for text, _ in
                               agmux.usage_report(limits=None, opencode=usage))
            self.assertIn("from session", report)

        with tempfile.TemporaryDirectory(prefix="agmux-oc-tokens-") as directory:
            db = Path(directory) / "opencode.db"
            connection = sqlite3.connect(db)
            connection.execute(
                "create table session_v2 (title text, cost real, model text, "
                "time_updated integer, tokens_input integer, tokens_output integer, "
                "tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer)")
            connection.execute(
                "insert into session_v2 values ('New schema',0.1,null,1700000000123,"
                "100,20,30,400,5)")
            connection.commit()
            connection.close()
            with patch.object(agmux, "OPENCODE_DB", db):
                agmux._OPENCODE_CACHE["at"] = 0.0
                usage = agmux.opencode_usage()
            agmux._OPENCODE_CACHE["at"] = 0.0
            self.assertEqual(usage["latest"]["tokens"], 555)
            self.assertEqual(usage["latest"]["reasoning"], 30)
            self.assertEqual(usage["latest"]["cache_read"], 400)
            self.assertEqual(usage["latest"]["updated"], 1700000000)

    def test_codex_usage_accepts_top_level_rate_limits(self):
        with tempfile.TemporaryDirectory(prefix="agmux-codex-") as directory:
            home = Path(directory)
            day = home / "sessions" / "2026" / "01" / "01"
            day.mkdir(parents=True)
            rollout = day / "rollout-2026-01-01T00-00-00-top.jsonl"
            rollout.write_text(json.dumps({"rate_limits": {
                "plan_type": "pro",
                "primary": {"used_percent": 1.0, "window_minutes": 300}}}) + "\n")
            with patch.object(agmux, "CODEX_HOME", home):
                limits = agmux.find_codex_rate_limits()
            self.assertEqual(limits["plan_type"], "pro")

    def test_missing_usage_is_graceful(self):
        with tempfile.TemporaryDirectory(prefix="agmux-usage-") as directory:
            with patch.object(agmux, "CODEX_HOME", Path(directory)), \
                    patch.object(agmux, "opencode_usage", return_value=None):
                self.assertIsNone(agmux.find_codex_rate_limits())
                self.assertEqual(agmux.usage_inline(None), "")
                self.assertEqual(agmux.opencode_inline(None), "")
                self.assertTrue(agmux.usage_report(limits=None))

    def test_money_formats_zero_and_small_amounts(self):
        self.assertEqual(agmux.money(0), "$0")
        self.assertEqual(agmux.money(None), "$0")
        self.assertEqual(agmux.money(0.0012), "$0.0012")
        self.assertEqual(agmux.money(0.24), "$0.24")


class Notifications(unittest.TestCase):
    def test_blocked_transition_reenables_the_same_prompt(self):
        prompt, previous = agmux.blocked_transition(None, "blocked", "Allow command?")
        self.assertEqual(prompt, "Allow command?")
        self.assertEqual(previous, "Allow command?")
        # Still blocked on the same prompt: no duplicate notification.
        self.assertEqual(agmux.blocked_transition(previous, "blocked", "Allow command?"),
                         (None, "Allow command?"))
        # Leaving blocked forgets the prompt...
        self.assertEqual(agmux.blocked_transition(previous, "working", ""), (None, None))
        # ...so the same prompt is announced again next time.
        self.assertEqual(agmux.blocked_transition(None, "blocked", "Allow command?"),
                         ("Allow command?", "Allow command?"))
        self.assertEqual(agmux.blocked_transition(None, "working", ""), (None, None))

    def test_custom_notify_command_receives_fields(self):
        with tempfile.TemporaryDirectory(prefix="agmux-notify-") as directory:
            output = Path(directory) / "out.txt"
            script = Path(directory) / "notify.sh"
            script.write_text("#!/bin/bash\nprintf '%s\\n' \"$1\" > " + agmux.shlex.quote(str(output)) + "\n")
            script.chmod(0o755)
            env = dict(os.environ, AGMUX_NOTIFY_CMD=f"{script} '{{title}}|{{body}}'")
            with patch.dict(os.environ, env, clear=True):
                agmux.send_notification("Codex", "work", 0, 65, True)
                for _ in range(100):
                    if output.exists():
                        break
                    time.sleep(0.02)
            self.assertTrue(output.exists(), "custom notify command did not run")
            self.assertIn("Codex finished", output.read_text())
            self.assertIn("work", output.read_text())

    def test_notification_disabled_and_default(self):
        with tempfile.TemporaryDirectory(prefix="agmux-notify-") as directory:
            output = Path(directory) / "out.txt"
            script = Path(directory) / "notify.sh"
            script.write_text("#!/bin/bash\ntouch " + agmux.shlex.quote(str(output)) + "\n")
            script.chmod(0o755)
            env = dict(os.environ, AGMUX_NOTIFY_CMD=str(script))
            with patch.dict(os.environ, env, clear=True):
                agmux.send_notification("Codex", "work", 0, 1, False)
            time.sleep(0.1)
            self.assertFalse(output.exists())
        with patch.dict(os.environ, {"AGMUX_NOTIFY": "off"}, clear=False):
            self.assertFalse(agmux.notify_default())
        with patch.dict(os.environ, {"AGMUX_NOTIFY": "1"}, clear=False):
            self.assertTrue(agmux.notify_default())

    def test_launch_agent_records_and_execs_shell(self):
        # os.execv is patched so the test process is preserved, and Tmux is
        # forced to fail so status recording is a safe no-op regardless of
        # whether the suite itself runs inside tmux.
        with patch.object(agmux.os, "execv") as execv, \
                patch.object(agmux.subprocess, "Popen") as popen, \
                patch.object(agmux, "Tmux", side_effect=agmux.Error("no tmux")):
            popen.return_value.wait.return_value = None
            popen.return_value.returncode = 0
            agmux.launch_agent("codex", "/bin/true", "/bin/bash", "s1", ["--flag"], False)
        execv.assert_called_once_with("/bin/bash", ["/bin/bash", "-l"])
        popen.assert_called_once_with(["/bin/true", "--flag"])


class Resources(unittest.TestCase):
    def test_humanize_size_and_label(self):
        self.assertEqual(agmux.humanize_size(0), "0K")
        self.assertEqual(agmux.humanize_size(512), "512K")
        self.assertEqual(agmux.humanize_size(2048), "2.0M")
        self.assertEqual(agmux.humanize_size(300 * 1024), "300M")
        self.assertEqual(agmux.humanize_size(2 * 1024 * 1024), "2.0G")
        self.assertEqual(agmux.resource_label(0, 0), "")
        self.assertEqual(agmux.resource_label(12.4, 1024), "cpu 12% \u00b7 mem 1.0M")
        self.assertEqual(agmux.resource_label(0.1, 1024), "mem 1.0M")
        self.assertEqual(agmux.resource_label(3.0, 0), "cpu 3%")
        # The label must say what it is, so it reads as machine use and not as
        # token or context-window usage.
        label = agmux.resource_label(12.4, 1024)
        self.assertIn("cpu", label)
        self.assertIn("mem", label)

    def test_process_table_falls_back_to_bsd_ps(self):
        linux = subprocess.CompletedProcess([], 1, "", "illegal option")
        bsd = subprocess.CompletedProcess([], 0, "10 1 2.5 100\n11 10 1.5 50\n", "")
        with patch.object(agmux.subprocess, "run", side_effect=[linux, bsd]) as run:
            agmux._PS_CACHE["at"] = 0.0
            table = agmux._process_table(max_age=0)
        self.assertEqual(table[10], (1, 2.5, 100))
        self.assertEqual(table[11], (10, 1.5, 50))
        self.assertEqual(run.call_count, 2)
        self.assertIn("-A", run.call_args_list[1].args[0])

    def test_process_table_empty_when_ps_unavailable(self):
        failed = subprocess.CompletedProcess([], 1, "", "not found")
        with patch.object(agmux.subprocess, "run", return_value=failed):
            agmux._PS_CACHE["at"] = 0.0
            self.assertEqual(agmux._process_table(max_age=0), {})
        agmux._PS_CACHE["at"] = 0.0

    def test_process_usage_sums_only_the_tree(self):
        table = {
            10: (1, 5.0, 100),
            11: (10, 2.0, 50),
            12: (11, 1.0, 25),
            13: (1, 9.0, 999),   # unrelated sibling
        }
        cpu, rss = agmux.process_usage([10], table)
        self.assertEqual(cpu, 8.0)
        self.assertEqual(rss, 175)

    def test_resources_toggle(self):
        with patch.dict(os.environ, {"AGMUX_RESOURCES": "0"}):
            self.assertFalse(agmux.resources_enabled())
        with patch.dict(os.environ, {"AGMUX_RESOURCES": "1"}):
            self.assertTrue(agmux.resources_enabled())


class Config(unittest.TestCase):
    def test_global_flags_work_before_and_after_subcommands(self):
        parser = agmux.build_parser()
        for argv in (["--no-notify", "new", "x"], ["new", "x", "--no-notify"],
                     ["--no-notify", "watch"], ["watch", "--no-notify"]):
            self.assertTrue(parser.parse_args(argv).no_notify, argv)
        for argv in (["--no-resources", "list"], ["list", "--no-resources"],
                     ["--no-resources", "info", "x"],
                     ["info", "x", "--no-resources"]):
            self.assertTrue(parser.parse_args(argv).no_resources, argv)

    def test_load_config_sets_unset_variables_only(self):
        with tempfile.TemporaryDirectory(prefix="agmux-config-") as directory:
            path = Path(directory) / "config"
            path.write_text("# a comment\n"
                            "repos = ~/work:~/code\n"
                            "agent = opencode\n"
                            "unknown = ignored\n"
                            "resources = 0\n")
            env = {k: v for k, v in os.environ.items() if k != "AGMUX_AGENT"}
            env["AGMUX_REPOS"] = "/already/set"
            with patch.dict(os.environ, env, clear=True):
                applied = agmux.load_config(path)
            self.assertEqual(applied.get("AGMUX_AGENT"), "opencode")
            self.assertEqual(applied.get("AGMUX_RESOURCES"), "0")
            self.assertNotIn("AGMUX_REPOS", applied)   # the environment wins

    def test_repos_roots_splits_pathseps_and_expands_home(self):
        value = f"~/a{os.pathsep}~/b{os.pathsep} ~/c "
        with patch.dict(os.environ, {"AGMUX_REPOS": value}):
            roots = agmux.repos_roots()
        self.assertEqual(roots, [Path.home() / "a", Path.home() / "b", Path.home() / "c"])

    def test_repos_merges_multiple_roots_without_duplicates(self):
        with tempfile.TemporaryDirectory(prefix="agmux-repos-") as directory:
            root = Path(directory)
            first, second = root / "one", root / "two"
            (first / "alpha").mkdir(parents=True)
            (second / "beta").mkdir(parents=True)
            # The same root listed twice must not duplicate its projects.
            value = os.pathsep.join((str(first), str(first), str(second)))
            with patch.dict(os.environ, {"AGMUX_REPOS": value}):
                repos = agmux.repos()
            self.assertEqual(sorted(repo["name"] for repo in repos), ["alpha", "beta"])
            # Two roots may hold same-named projects; the path disambiguates.
            (second / "alpha").mkdir()
            with patch.dict(os.environ, {"AGMUX_REPOS": f"{first}{os.pathsep}{second}"}):
                repos = agmux.repos()
            self.assertEqual(sorted(repo["name"] for repo in repos),
                             ["alpha", "alpha", "beta"])
            self.assertEqual(len({repo["id"] for repo in repos}), 3)

    def test_repos_reports_when_no_root_exists(self):
        with patch.dict(os.environ, {"AGMUX_REPOS": "/nonexistent/agmux-repos"}):
            with self.assertRaises(agmux.Error):
                agmux.repos()


class Help(unittest.TestCase):
    def test_help_covers_current_bindings_and_commands(self):
        text = "\n".join("".join(part for _, part in line)
                         for line in agmux.build_help_lines(100))
        for expected in ("Alt-s", "Alt-x", "Ctrl-b Ctrl-s", "Ctrl-O", "Ctrl-P",
                         "Ctrl-G", "Ctrl-N", "Ctrl-R", "Ctrl-D", "Ctrl-W",
                         "agmux doctor", "agmux list --json",
                         "agmux info NAME --json", "agmux usage --json",
                         "no telemetry"):
            self.assertIn(expected, text)

    def test_help_wraps_to_the_terminal_width(self):
        for width in (40, 60, 80, 100, 140):
            for line in agmux.build_help_lines(width):
                rendered = "".join(part for _, part in line)
                self.assertLessEqual(len(rendered), width, (width, rendered))

    def test_help_uses_a_single_aligned_description_column(self):
        lines = agmux.build_help_lines(100)
        starts = set()
        for segments in lines:
            if len(segments) == 2 and segments[0][0] == "key":
                starts.add(len(segments[0][1]))
        self.assertEqual(len(starts), 1, starts)


if __name__ == "__main__":
    unittest.main()
