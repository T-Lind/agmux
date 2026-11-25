import contextlib
from concurrent.futures import ThreadPoolExecutor
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
from unittest.mock import Mock, patch


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
                        "path = Path(os.environ['AGMUX_TEST_ARGV'])\n"
                        "temp = path.with_suffix('.' + str(os.getpid()))\n"
                        "temp.write_text(json.dumps(sys.argv[1:])); temp.replace(path)\n")
        fake.chmod(0o755)
        env = dict(os.environ, AGMUX_TMUX_SOCKET=self.socket, AGMUX_TEST_ARGV=str(self.record),
                   AGMUX_STATE_DIR=str(self.root / "state"), AGMUX_SAVE_INTERVAL="0",
                   AGMUX_PERSIST="1", AGMUX_WATCH_INTERVAL="1", AGMUX_NOTIFY_CMD="/bin/true",
                   CODEX_HOME=str(self.root / "codex-home"), CODEX_SQLITE_HOME=str(self.root / "codex-home"),
                   OPENCODE_DATA_DIR=str(self.root / "opencode"), CLAUDE_CONFIG_DIR=str(self.root / "claude"),
                   PATH=str(self.bin) + os.pathsep + os.environ["PATH"], SHELL="/bin/bash")
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        env.pop("AIDER_CHAT_HISTORY_FILE", None)
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

    def codex_args(self):
        actual = json.loads(self.record.read_text())
        self.assertEqual(actual[:2], ["-c", agmux._CODEX_TITLE])
        return actual[2:]

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

    def test_wizard_selects_most_used_project_before_more_recent_and_unused(self):
        hot = self.root / "z-frequent"
        earlier = self.root / "mid-earlier"
        later = self.root / "a-later"
        unused = self.root / "aa-unused"
        for directory in (hot, earlier, later, unused):
            directory.mkdir()
        with patch.dict(os.environ, {"AGMUX_REPOS": str(self.root)}):
            for i, directory in enumerate((hot, hot, earlier, later)):
                with contextlib.redirect_stdout(io.StringIO()):
                    agmux.main(["new", f"test-{i}", "--cwd", str(directory), "--agent", "shell", "--detach"])
            with self.assertRaises(agmux.Error):
                self.tmux.create("test-3", later, agent="shell")
            self.tmux.delete(self.tmux.resolve("test-3")["id"])
            self.tmux.restore("test-3")
            with patch.object(agmux, "choose", side_effect=["shell", str(hot)]) as choose, \
                    patch.object(agmux, "prompt", return_value="from-wizard"):
                agmux.new_session_flow(None, self.tmux, None)
        choices = choose.call_args_list[1].args[2]
        paths = [row["value"] for row in choices]
        self.assertEqual(paths[:3], list(map(str, (hot, later, earlier))))
        self.assertIn("2 sessions", choices[0]["note"])
        self.assertIn(str(unused), paths)
        agent, uses = agmux.project_history(self.tmux)
        self.assertEqual(agent, "shell")
        self.assertEqual(uses[str(hot)], 3)
        self.assertEqual(uses[str(later)], 1)

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
        self.assertEqual(self.codex_args(), arguments)
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
        self.assertEqual(self.codex_args(),
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

    def start_paused_chat(self):
        keys = self.root / "chat-keys"
        sid = self.tmux.create("paused-chat", self.root, agent="shell")
        pane = self.tmux.run("list-panes", "-s", "-t", sid, "-F", "#{pane_id}")
        self.tmux.run("respawn-pane", "-k", "-t", pane, agmux.sys.executable,
                      str(MODULE.parent / "tests/fake_chat.py"), str(keys))
        self.tmux.session_option(sid, "agmux_agent", "codex")
        for _ in range(100):
            if "OLD_CHAT_HISTORY" in "\n".join(self.tmux.pane_lines(pane)):
                break
            time.sleep(0.02)
        self.assertIn("OLD_CHAT_HISTORY", "\n".join(self.tmux.pane_lines(pane)))
        return sid, pane, keys

    def test_picker_follow_resets_agent_scroll_and_preserves_draft(self):
        sid, pane, keys = self.start_paused_chat()
        # Captures alone remain read-only, including captures used by peek/state.
        self.assertIn("OLD_CHAT_HISTORY", "\n".join(self.tmux.preview(sid)))
        self.assertEqual(keys.read_bytes(), b"")
        self.assertTrue(self.tmux.follow_preview(sid))
        for _ in range(100):
            screen = "\n".join(self.tmux.pane_lines(pane))
            if "LIVE_CHAT_MARKER" in screen:
                break
            time.sleep(0.02)
        self.assertIn("LIVE_CHAT_MARKER", screen)
        self.assertNotIn("OLD_CHAT_HISTORY", screen)
        self.assertIn("DRAFT_STAYS_UNSUBMITTED", screen)
        self.assertEqual(keys.read_bytes(), b"\x1b[1;5F")
        self.assertFalse(self.tmux.follow_preview(sid))
        self.assertEqual(keys.read_bytes(), b"\x1b[1;5F")

    def test_picker_follow_respects_opt_out_unknown_agent_and_tmux_copy_mode(self):
        sid, pane, keys = self.start_paused_chat()
        with patch.dict(os.environ, {"AGMUX_FOLLOW_PREVIEW": "0"}):
            self.assertFalse(self.tmux.follow_preview(sid))
        self.tmux.session_option(sid, "agmux_agent", "claude")
        self.assertFalse(self.tmux.follow_preview(sid))
        self.tmux.session_option(sid, "agmux_agent", "codex")
        self.tmux.run("copy-mode", "-t", pane)
        self.assertFalse(self.tmux.follow_preview(sid))
        self.assertEqual(keys.read_bytes(), b"")
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", pane,
                                       "#{pane_in_mode}"), "1")

    def test_picker_follow_skips_other_attached_readers_but_allows_popup_origin(self):
        sid, pane, keys = self.start_paused_chat()
        original = self.tmux.run
        def with_reader(*args, **kwargs):
            if args[0] == "list-clients":
                return "/dev/pts/reader"
            return original(*args, **kwargs)
        with patch.object(self.tmux, "run", side_effect=with_reader):
            self.assertFalse(self.tmux.follow_preview(sid))
            self.assertFalse(self.tmux.follow_preview(sid, client="/dev/pts/someone-else"))
            self.assertEqual(keys.read_bytes(), b"")
            self.assertTrue(self.tmux.follow_preview(sid, client="/dev/pts/reader"))

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

    def test_recovery_survives_server_shutdown_with_layout_and_directories(self):
        directory = self.root / "project with spaces"
        directory.mkdir()
        sid = self.tmux.create("reboot work", directory, agent="shell")
        first = self.tmux.run("list-panes", "-t", sid, "-F", "#{pane_id}")
        second = self.tmux.run("split-window", "-h", "-t", first, "-c", str(self.root),
                               "-P", "-F", "#{pane_id}")
        self.tmux.run("select-pane", "-t", second)
        self.tmux.run("resize-pane", "-Z", "-t", second)
        self.tmux.shell_window(sid, self.root, "logs with spaces")
        self.tmux.checkpoint()
        before = self.tmux.workspace_store().read()["reboot work"]
        self.tmux.run("kill-server")
        self.tmux._store = None
        # A fresh agmux invocation against an empty server must retain the snapshot.
        for _ in range(50):
            if not self.tmux.server_identity():
                break
            time.sleep(0.02)
        self.tmux.checkpoint()
        self.assertEqual({r["name"] for r in agmux.saved_rows(self.tmux)}, {"seed", "reboot work"})
        restored = self.tmux.restore("reboot work")
        after = self.tmux.workspace_store().read()["reboot work"]
        self.assertEqual(len(after["windows"]), 2)
        self.assertEqual([len(w["panes"]) for w in after["windows"]], [2, 1])
        self.assertEqual([w["name"] for w in after["windows"]],
                         [w["name"] for w in before["windows"]])
        self.assertEqual([[p["cwd"] for p in w["panes"]] for w in after["windows"]],
                         [[p["cwd"] for p in w["panes"]] for w in before["windows"]])
        self.assertTrue(after["windows"][0]["zoomed"])
        self.assertEqual([w["active"] for w in after["windows"]],
                         [w["active"] for w in before["windows"]])
        # The first window had horizontal splits; retain that geometry.
        self.tmux.run("resize-pane", "-Z", "-t", f"{restored}:0")
        geometry = self.tmux.run("list-panes", "-t", f"{restored}:0", "-F",
                                 "#{pane_left}:#{pane_top}").splitlines()
        self.assertEqual(len(geometry), 2)
        self.assertNotEqual(geometry[0].split(":")[0], geometry[1].split(":")[0])
        self.assertEqual(geometry[0].split(":")[1], geometry[1].split(":")[1])
        self.assertNotIn("reboot work", [r["name"] for r in agmux.saved_rows(self.tmux)])

    def test_recovery_keeps_closed_sessions_and_moves_renamed_definitions(self):
        sid = self.tmux.create("old", self.root, agent="shell")
        self.tmux.rename(sid, "new")
        records = self.tmux.workspace_store().read()
        self.assertIn("new", records)
        self.assertNotIn("old", records)
        self.tmux.delete(sid)
        self.tmux.checkpoint()
        self.assertIn("new", [r["name"] for r in agmux.saved_rows(self.tmux)])
        self.tmux.workspace_store().forget("new")
        self.tmux.checkpoint()
        self.assertNotIn("new", self.tmux.workspace_store().read())

    def test_recovery_refuses_conflicts_missing_paths_and_rolls_back_failures(self):
        directory = self.root / "vanishing"
        directory.mkdir()
        sid = self.tmux.create("work", directory, agent="shell")
        with self.assertRaisesRegex(agmux.Error, "already running"):
            self.tmux.restore("work")
        self.tmux.delete(sid)
        directory.rmdir()
        with self.assertRaisesRegex(agmux.Error, "directory does not exist"):
            self.tmux.restore("work")
        self.assertNotIn("work", [r["name"] for r in self.tmux.sessions()])
        directory.mkdir()
        original = self.tmux.run
        def failing(*args, **kwargs):
            if args[0] == "select-layout":
                raise agmux.Error("layout failed")
            return original(*args, **kwargs)
        with patch.object(self.tmux, "run", side_effect=failing):
            with self.assertRaisesRegex(agmux.Error, "layout failed"):
                self.tmux.restore("work")
        self.assertNotIn("work", [r["name"] for r in self.tmux.sessions()])
        self.assertIn("work", self.tmux.workspace_store().read())
        self.assertEqual(self.tmux.resolve("seed")["name"], "seed")

    def test_recovery_never_replays_prompt_by_default_and_preserves_literal_args(self):
        sid = self.tmux.create("agent", self.root, agent="codex",
                               agent_args=["do a task", "$(touch PWNED)", ";"])
        for _ in range(100):
            if self.record.exists():
                break
            time.sleep(0.02)
        self.tmux.delete(sid)
        self.record.unlink()
        self.tmux.restore("agent")
        for _ in range(100):
            if self.record.exists():
                break
            time.sleep(0.02)
        self.assertEqual(self.codex_args(), ["resume"])
        self.assertFalse((self.root / "PWNED").exists())
        self.tmux.delete(self.tmux.resolve("agent")["id"])
        self.record.unlink()
        self.tmux.restore("agent", fresh=True)
        for _ in range(100):
            if self.record.exists():
                break
            time.sleep(0.02)
        self.assertEqual(self.codex_args(), ["do a task", "$(touch PWNED)", ";"])

    def test_recovery_uses_exact_explicit_codex_id(self):
        sid = self.tmux.create("exact", self.root, agent="codex", agent_args=["resume", "abc-123"])
        self.tmux.delete(sid)
        self.tmux.restore("exact")
        for _ in range(100):
            if self.record.exists() and self.codex_args() == ["resume", "abc-123"]:
                break
            time.sleep(0.02)
        self.assertEqual(self.codex_args(), ["resume", "abc-123"])
        self.assertEqual(self.tmux.workspace_store().read()["exact"]["resume_id"], "abc-123")

    def test_live_codex_identity_and_first_message_survive_abrupt_close(self):
        identity = "00000001-2222-3333-4444-555555555555"
        db_dir = Path(os.environ["CODEX_SQLITE_HOME"])
        db_dir.mkdir()
        db = db_dir / "state_5.sqlite"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE threads(id TEXT,first_user_message TEXT)")
            con.execute("INSERT INTO threads VALUES(?, '')", (identity,))
            con.execute("INSERT INTO threads VALUES(?, 'Another chat here')",
                        ("00000002-2222-3333-4444-555555555555",))
        self.tmux.create("Fix old bug", self.root, agent="shell")
        self.tmux.delete(self.tmux.resolve("Fix old bug")["id"])
        binary = self.bin / "codex"
        binary.write_text("#!/usr/bin/python3\nimport time\n"
                          f"print('\\033]2;Ready | fixture | {identity[:29]}...\\007', flush=True)\n"
                          "print('› Draft only',flush=True)\ntime.sleep(60)\n")
        sid = self.tmux.create("placeholder", self.root, "codex", notify=False, auto_name=True)
        for _ in range(150):
            if self.tmux.workspace_store().read()["placeholder"]["resume_id"] == identity:
                break
            time.sleep(.02)
        self.assertEqual(self.tmux.workspace_store().read()["placeholder"]["resume_id"], identity)
        self.assertEqual(self.tmux.resolve(sid)["name"], "placeholder")
        with sqlite3.connect(db) as con:
            con.execute("UPDATE threads SET first_user_message=? WHERE id=?",
                        ("Fix: old.\nbug", identity))
        for _ in range(150):
            if "Fix old bug 2" in self.tmux.workspace_store().read():
                break
            time.sleep(.02)
        self.assertEqual(self.tmux.resolve(sid)["name"], "Fix old bug 2")
        # Alt-x talks directly to tmux, bypassing the agmux delete checkpoint.
        self.tmux.run("kill-session", "-t", sid)
        saved = self.tmux.workspace_store().read()
        self.assertNotIn("placeholder", saved)
        self.assertEqual(saved["Fix old bug 2"]["resume_id"], identity)
        self.assertFalse(saved["Fix old bug 2"]["auto_name"])
        self.assertIn("Fix old bug", saved)
        binary.write_text("#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\n"
                          f"Path({str(self.record)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        self.tmux.restore("Fix old bug 2", notify=False)
        for _ in range(100):
            if self.record.exists() and self.record.stat().st_size:
                break
            time.sleep(.02)
        self.assertEqual(self.codex_args(), ["resume", identity])

    def test_new_and_restored_harnesses_receive_the_same_id(self):
        for agent, flag in (("opencode", "--session"), ("claude", "--resume"),
                            ("gemini", "--resume")):
            with self.subTest(agent=agent):
                binary = self.bin / agent
                output = self.root / agent
                binary.write_text("#!/usr/bin/python3\nimport sys,json,time\nfrom pathlib import Path\n"
                                  "if '--help' in sys.argv:\n"
                                  " print('--session ID to continue, or to create if it does not exist; --session-id'); exit()\n"
                                  f"Path({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\ntime.sleep(30)\n")
                binary.chmod(0o755)
                sid = self.tmux.create(agent, self.root, agent, notify=False)
                identity = self.tmux.workspace_store().read()[agent]["resume_id"]
                self.assertTrue(identity)
                for _ in range(100):
                    if output.exists() and output.stat().st_size:
                        break
                    time.sleep(.02)
                self.assertEqual(json.loads(output.read_text())[-1], identity)
                self.tmux.run("kill-session", "-t", sid)
                output.unlink()
                self.tmux.restore(agent, notify=False)
                for _ in range(100):
                    if output.exists() and output.stat().st_size:
                        break
                    time.sleep(.02)
                self.assertEqual(json.loads(output.read_text()), [flag, identity])

    def test_aider_sessions_in_one_directory_keep_separate_native_histories(self):
        binary = self.bin / "aider"
        binary.write_text("#!/usr/bin/python3\nimport sys,time\nfrom pathlib import Path\n"
                          "path = Path(sys.argv[sys.argv.index('--chat-history-file')+1])\n"
                          "if '--restore-chat-history' in sys.argv: print(path.read_text(),flush=True)\n"
                          "else: path.write_text('fixture ' + path.stem)\ntime.sleep(30)\n")
        binary.chmod(0o755)
        first = self.tmux.create("aider-one", self.root, "aider", notify=False)
        self.tmux.create("aider-two", self.root, "aider", notify=False)
        records = self.tmux.workspace_store().read()
        history = Path(records["aider-one"]["history_file"])
        self.assertNotEqual(str(history), records["aider-two"]["history_file"])
        for _ in range(100):
            if history.exists():
                break
            time.sleep(.02)
        self.tmux.delete(first)
        restored = self.tmux.restore("aider-one", notify=False)
        for _ in range(100):
            output = self.tmux.visible_lines(restored)
            if "fixture " + history.stem in output:
                break
            time.sleep(.02)
        self.assertIn("fixture " + history.stem, output)

    def test_manual_rename_cancels_pending_prompt_name(self):
        sid = self.tmux.create("default-name", self.root, "shell")
        self.tmux.session_option(sid, "agmux_auto_name", "default-name")
        self.tmux.rename(sid, "my name")
        self.assertFalse(self.tmux.name_from_message(sid, "default-name", "Late first message"))
        self.assertEqual(self.tmux.resolve(sid)["name"], "my name")
        self.assertFalse(self.tmux.workspace_store().read()["my name"]["auto_name"])

    def test_recovery_captures_complete_codex_exit_id_but_not_wrapped_prefix(self):
        identity = "11111111-2222-3333-4444-555555555555"
        for name, output, expected in (
                ("complete", identity, identity),
                ("wrapped", identity[:18] + "\n" + identity[18:], "")):
            with self.subTest(name=name):
                (self.bin / "codex").write_text(
                    "#!/usr/bin/python3\nprint(" + repr(
                        "To continue this session, run codex resume " + output) + ")\n")
                sid = self.tmux.create(name, self.root, agent="codex")
                for _ in range(100):
                    row = self.tmux.resolve(sid)
                    command = self.tmux.run("display-message", "-p", "-t", sid,
                                            "#{pane_current_command}")
                    if row["finished"] and command == "bash":
                        break
                    time.sleep(0.02)
                self.assertTrue(row["finished"])
                self.tmux.checkpoint()
                self.assertEqual(self.tmux.workspace_store().read()[name]["resume_id"], expected)

    def test_recovery_storage_is_private_atomic_and_separate_per_socket(self):
        self.tmux.create("private", self.root, agent="shell")
        store = self.tmux.workspace_store()
        previous = store.path.read_bytes()
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotEqual(store.path, agmux.WorkspaceStore(self.socket + "-other").path)
        with patch.object(agmux.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.tmux.checkpoint()
        self.assertEqual(store.path.read_bytes(), previous)
        self.assertEqual(list(store.path.parent.glob(".workspace-*")), [])
        store.path.write_text("{broken")
        with self.assertRaisesRegex(agmux.Error, "Cannot read recovery file"):
            self.tmux.checkpoint()
        self.assertEqual(store.path.read_text(), "{broken")

    def test_recovery_cli_restore_all_continues_past_missing_directory(self):
        missing = self.root / "gone"
        missing.mkdir()
        one = self.tmux.create("gone", missing, agent="shell")
        two = self.tmux.create("good", self.root, agent="shell")
        self.tmux.delete(one)
        self.tmux.delete(two)
        missing.rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(agmux.Error, "gone"):
                agmux.main(["restore", "--all"])
        self.assertEqual(self.tmux.resolve("good")["name"], "good")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(agmux.main(["saved", "--json"]), 0)
        self.assertEqual([r["name"] for r in json.loads(output.getvalue())], ["gone"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agmux.main(["forget", "gone", "--yes"]), 0)
        self.assertEqual(agmux.saved_rows(self.tmux), [])

    def test_recovery_bypasses_custom_default_command_and_keeps_window_indices(self):
        self.tmux.run("set-option", "-g", "base-index", "1")
        sid = self.tmux.create("indexed", self.root, agent="shell")
        self.tmux.shell_window(sid, self.root, "second")
        self.tmux.run("move-window", "-s", f"{sid}:2", "-t", f"{sid}:7")
        self.tmux.run("split-window", "-h", "-t", f"{sid}:7", "-c", str(self.root))
        self.tmux.delete(sid)
        marker = self.root / "SHOULD_NOT_RUN"
        self.tmux.run("set-option", "-g", "default-command", "touch " + str(marker))
        restored = self.tmux.restore("indexed")
        indices = self.tmux.run("list-windows", "-t", restored, "-F", "#{window_index}")
        self.assertEqual(indices.splitlines(), ["1", "7"])
        time.sleep(0.1)
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.tmux.describe(restored)["panes"]), 3)

    def test_recovery_can_be_disabled(self):
        with patch.dict(os.environ, {"AGMUX_PERSIST": "0"}):
            self.tmux.create("untracked", self.root, agent="shell")
            self.assertFalse(self.tmux.workspace_store().path.exists())
            self.assertEqual(agmux.saved_rows(self.tmux), [])

    def test_background_saver_captures_changes_without_picker_and_stops_with_server(self):
        self.tmux.checkpoint()
        server = self.tmux.server_identity()
        args = [agmux.sys.executable, str(MODULE), "_save-loop", "--socket", self.socket,
                "--state-dir", str(self.root / "state"), "--server", server]
        env = dict(os.environ, AGMUX_SAVE_INTERVAL="2")
        worker = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # Wait until the saver has taken its lifetime lock and first snapshot.
            time.sleep(0.2)
            duplicate = subprocess.run(args, env=env, capture_output=True, timeout=3)
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
            self.assertIsNone(worker.poll())
            self.tmux.run("new-session", "-d", "-s", "outside", "-c", str(self.root))
            deadline = time.monotonic() + 5
            while "outside" not in self.tmux.workspace_store().read() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("outside", self.tmux.workspace_store().read())
            self.tmux.run("kill-server")
            output, errors = worker.communicate(timeout=4)
            self.assertEqual(worker.returncode, 0, errors)
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=3)



class PromptHelpers(unittest.TestCase):
    def test_project_name_match_precedes_fuzzy_path_match(self):
        screen = Mock()
        screen.getmaxyx.return_value = (24, 80)
        screen.get_wch.side_effect = list("demo\r")
        options = [dict(label="daily", note="12 sessions /home/repos"),
                   dict(label="demo-project", note="/repos/demo-project")]
        with patch.object(agmux, "put"):
            self.assertEqual(agmux.choose(screen, "Project", options, Mock(), paths=True), "demo-project")

    def test_complete_path_extends_to_unique_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "alpha-one").mkdir()
            (Path(raw) / "alpha-two").mkdir()
            (Path(raw) / "beta").mkdir()
            self.assertEqual(agmux.complete_path(raw + "/al"), raw + "/alpha-")
            self.assertEqual(agmux.complete_path(raw + "/be"), raw + "/beta/")
            self.assertEqual(agmux.complete_path(raw + "/zz"), raw + "/zz")

    def test_project_history_seeds_saved_workspaces_and_legacy_recents_once(self):
        with tempfile.TemporaryDirectory() as raw, \
                patch.dict(os.environ, {"AGMUX_STATE_DIR": raw, "AGMUX_PERSIST": "1"}), \
                patch.object(agmux.Tmux, "workspace_store") as store:
            workspace = agmux.Tmux()
            hot = Path(raw) / "popular"
            hot.mkdir()
            alias = Path(raw) / "alias"
            alias.symlink_to(hot)
            path = Path(raw) / "recent.json"
            recent = ["/recent", str(alias), "/recent"]
            path.write_text(json.dumps({"agent": "codex", "dirs": recent}))
            store.return_value.read.return_value = {"one": {"cwd": str(hot)}, "two": {"cwd": str(alias)}}
            agent, uses = agmux.project_history(workspace)
            self.assertEqual(agent, "codex")
            self.assertEqual(list(uses.items()), [("/recent", 1), (str(hot), 2)])
            store.return_value.read.side_effect = AssertionError("must only import old counts once")
            for i in range(10):
                agmux.project_history(workspace, cwd=f"/new-{i}")
            agent, uses = agmux.project_history(workspace)
            self.assertEqual(agent, "codex")
            self.assertEqual(uses[str(hot)], 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_project_launches_keep_every_count(self):
        with tempfile.TemporaryDirectory() as raw, \
                patch.dict(os.environ, {"AGMUX_STATE_DIR": raw, "AGMUX_PERSIST": "0"}):
            tmux = agmux.Tmux()
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: agmux.project_history(tmux, cwd="/same"), range(24)))
            self.assertEqual(agmux.project_history(tmux), ("", {"/same": 24}))

    def test_fit_pads_and_truncates(self):
        self.assertEqual(agmux.fit("abc", 5), "abc  ")
        self.assertEqual(len(agmux.fit("abcdefghij", 5)), 5)


class AgentMetadata(unittest.TestCase):
    def test_harness_resume_arguments_and_legacy_fallbacks(self):
        identity = "00000001-2222-3333-4444-555555555555"
        cases = [
            ("claude", ["--resume=" + identity, "old prompt"], ["--resume", identity]),
            ("crush", ["-s", identity], ["--session", identity]),
            ("goose", ["session", "-r", "--session-id", "20261125_2"],
             ["session", "--resume", "--session-id", "20261125_2"]),
            ("gemini", ["-r", identity], ["--resume", identity]),
            ("opencode", ["--session=ses_123"], ["--session", "ses_123"]),
            ("opencode", ["--server", "http://localhost:4096", "-s", "ses_123", "--prompt", "old"],
             ["--session", "ses_123", "--server", "http://localhost:4096"]),
            ("aider", ["--chat-history-file", "my chat.md", "--message", "old prompt"],
             ["--chat-history-file", "my chat.md", "--restore-chat-history"]),
            ("codex", ["resume", "--last"], ["resume"]),
            ("claude", ["--continue"], ["--resume"]),
        ]
        for agent, args, expected in cases:
            with self.subTest(agent=agent, args=args):
                record = dict(agent=agent, argv=args, resume_id="")
                self.assertEqual(agmux.recovery_launch(record), (agent, expected))
                self.assertEqual(agmux.recovery_launch(record, fresh=True), (agent, args))
        for agent, args in (("gemini", ["--resume", "1"]), ("gemini", ["-r", "latest"]),
                            ("opencode", ["--continue"]), ("goose", []), ("aider", []),
                            ("crush", ["--continue"]), ("shell", [])):
            self.assertEqual(agmux.recovery_launch(dict(agent=agent, argv=args, resume_id="")),
                             ("shell", []))

    def test_truncated_codex_id_requires_one_exact_native_match(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CODEX_SQLITE_HOME": temp}):
            prefix = "00000001-2222-3333-4444-55555"
            with sqlite3.connect(Path(temp) / "state_5.sqlite") as db:
                db.execute("CREATE TABLE threads(id TEXT)")
                db.executemany("INSERT INTO threads VALUES(?)", [(prefix + "1" * 7,), (prefix + "2" * 7,)])
            self.assertEqual(agmux.codex_identity("Ready | chat | " + prefix + "..."), "")
            self.assertEqual(agmux.codex_identity("Ready | " + prefix + "1"*7 + " | " + prefix + "..."),
                             prefix + "1"*7)

    def test_first_message_uses_user_role_and_exact_harness_id(self):
        with tempfile.TemporaryDirectory() as temp:
            identity = "00000001-2222-3333-4444-555555555555"
            projects = Path(temp) / "projects" / "example"
            projects.mkdir(parents=True)
            messages = [{"type": "assistant", "message": {"content": "Ignore"}},
                        {"type": "user", "isMeta": True, "message": {"content": "Metadata"}},
                        {"type": "user", "message": {"content": [{"type": "text", "text": "First question"}]}},
                        {"type": "user", "message": {"content": "Second question"}}]
            (projects / (identity + ".jsonl")).write_text("\n".join(map(json.dumps, messages)))
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temp}):
                self.assertEqual(agmux.first_message("claude", identity), "First question")
            path = Path(temp) / "opencode.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE session_message(session_id TEXT,type TEXT,seq INTEGER,data TEXT)")
                db.executemany("INSERT INTO session_message VALUES(?,?,?,?)", [
                    ("ses_other", "user", 0, '{"text":"Other"}'), ("ses_test", "assistant", 0, '{"text":"Reply"}'),
                    ("ses_test", "user", 2, '{"text":"Second"}'), ("ses_test", "user", 1, '{"text":"First"}')])
            with patch.object(agmux, "OPENCODE_DB", path):
                self.assertEqual(agmux.first_message("opencode", "ses_test"), "First")
    def test_paused_chat_control_detection_is_specific(self):
        for line in ("New activity · ↓ Back to bottom · esc", "↓ Bottom", "↓ Back to bottom"):
            self.assertIsNotNone(agmux._CODEX_PAUSED_VIEW.fullmatch(line))
        for line in ("I wrote Back to bottom in my draft", "↓", "Select ↓ Bottom to approve",
                     "› ↓ Bottom", "Search: ↓ Bottom"):
            self.assertIsNone(agmux._CODEX_PAUSED_VIEW.fullmatch(line))

    def test_resume_id_never_uses_last_or_an_option(self):
        self.assertEqual(agmux.explicit_codex_resume(["resume", "--last"]), "")
        self.assertEqual(agmux.explicit_codex_resume(["resume", "--all"]), "")
        self.assertEqual(agmux.explicit_codex_resume(["resume", "abc-123"]), "abc-123")

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
    def test_codex_live_questions_work_and_terminal_exit(self):
        title = "⠴ Working | fixture | 00000001-2222-3333-4444-55555..."
        quiz = ["Question 1/2 (2 unanswered)", "Which format should I use?", "› 1. Plain",
                "2. JSON", "tab to add notes | enter to submit answer | esc to interrupt"] + [""] * 40
        self.assertEqual(agmux.agent_state("codex", quiz, title), ("blocked", "Which format should I use?"))
        self.assertEqual(agmux.agent_state("codex", ["1 question · shift+left to answer"], title)[0], "blocked")
        for mark in ("!", "."):
            self.assertEqual(agmux.agent_state("codex", [], f"[ {mark} ] Action Required")[0], "blocked")
        ready = "Ready | fixture | 00000001-2222-3333-4444-55555..."
        question = ("id", "completed", 123, "Which format should I use?")
        self.assertEqual(agmux.agent_state("codex", [], ready, question), ("blocked", question[3]))
        self.assertEqual(agmux.agent_state("codex", [], title, question)[0], "working")
        self.assertEqual(agmux.agent_state("codex", [], ready, ("id2", "inProgress", 124, ""))[0], "working")
        self.assertEqual(agmux.agent_state("codex", [], ready, ("id3", "interrupted", 125, ""))[0], "idle")
        for state, status in (("blocked", "blocked"), ("working", "running"), ("idle", "exited")):
            self.assertEqual(agmux.row_status(dict(status="exited", unread=1, state=state)), status)

    def test_active_codex_goal_keeps_a_completed_step_running(self):
        identity = "00000001-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CODEX_SQLITE_HOME": temp}):
            with sqlite3.connect(Path(temp) / "thread_history_1.sqlite") as db:
                db.execute("CREATE TABLE thread_turns(thread_id,turn_id,status,started_at,final_agent_item_id,rollout_ordinal)")
                db.execute("CREATE TABLE thread_items(thread_id,turn_id,item_id,item_json)")
                db.execute("INSERT INTO thread_turns VALUES(?, 'step', 'completed', 123, NULL, 1)", (identity,))
            with sqlite3.connect(Path(temp) / "goals_1.sqlite") as db:
                db.execute("CREATE TABLE thread_goals(thread_id,status)")
                db.execute("INSERT INTO thread_goals VALUES(?, 'active')", (identity,))
            self.assertEqual(agmux.agent_state("codex", [], "Ready", agmux.codex_turn(identity))[0], "working")
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

    def test_classify_screen_per_agent_dialogs(self):
        cases = {
            "codex": ["  Would you like to run the following command?", "$ rm -rf build",
                      "› 1. Yes, proceed (y)", "  Press enter to confirm or esc to cancel"],
            "claude": [" Bash command", "   npm test", " Do you want to proceed?",
                       " ❯ 1. Yes", "   2. No", " Esc to cancel · Tab to amend"],
            "opencode": ["△ Permission required", "  Allow once   Always allow   Reject"],
            "gemini": ["Allow execution of: 'ls'?", "● 1. Allow once",
                       "  3. No, suggest changes (esc)"],
            "aider": ["Run shell command? (Y)es/(N)o/(D)on't ask again [Yes]:"],
            "crush": ["Permission Required", "Allow", "Allow for session", "Deny"],
            "goose": ["Goose would like to call the above tool, confirm?"],
        }
        for agent, lines in cases.items():
            self.assertEqual(agmux.classify_screen(lines, agent=agent)[0], "blocked", agent)
        # Codex shows "esc to interrupt" on its question screen; it still needs you.
        self.assertEqual(agmux.classify_screen(
            ["Question 1/1", "enter to submit answer | esc to interrupt"], agent="codex")[0],
            "blocked")

    def test_classify_screen_ignores_prose_questions_above_idle_composer(self):
        for agent, lines in (
                ("claude", ["⏺ I can add tests. Should I continue?", "",
                            "╭" + "─" * 20, "│ > ", "╰" + "─" * 20,
                            "  ? for shortcuts"]),
                ("codex", ["• Do you want me to also update the docs?", "",
                           "› Ask Codex to do anything", "  ? for shortcuts"]),
                ("opencode", ["Can I help with anything else?", "  ctrl+p commands"])):
            self.assertEqual(agmux.classify_screen(lines, agent=agent)[0], "idle", agent)

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


class Clipboard(unittest.TestCase):
    def test_selection_copies_exact_substrings_forward_and_backward_without_ansi(self):
        lines = agmux.plain_lines([agmux.parse_sgr("\033[31mhello world\033[0m"),
                                   agmux.parse_sgr("next line")])
        self.assertEqual(agmux.selected_text(lines, (0, 6), (0, 10)), "world")
        self.assertEqual(agmux.selected_text(lines, (1, 3), (0, 6)), "world\nnext")
        self.assertEqual(agmux.selected_text(lines, (0, 6), (1, 3)), "world\nnext")
        self.assertEqual(agmux.selected_text([], (0, 0), (0, 0)), "")
        self.assertEqual(agmux.column_index("你ab", 2), 1)
        self.assertEqual(agmux.text_cells("a你e\u0301"), 4)

    def test_clipboard_uses_literal_stdin_and_falls_back_between_tools(self):
        contents = "$(touch NEVER)\nquotes ' ; 你好"
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}), \
                patch.object(agmux.shutil, "which", side_effect=lambda name: "/bin/" + name), \
                patch.object(agmux.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]) as run:
            self.assertIn("xclip", agmux.copy_text(contents))
        self.assertEqual(run.call_args.args[0], ["/bin/xclip", "-selection", "clipboard"])
        self.assertEqual(run.call_args.kwargs["input"], contents)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_clipboard_osc52_is_encoded_and_reports_terminal_delivery(self):
        contents = "hello\n你好\033]52;bad"
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(agmux.os, "write") as write, \
                patch.object(agmux.sys, "stdout") as stdout:
            stdout.fileno.return_value = 9
            self.assertIn("terminal clipboard", agmux.copy_text(contents))
        sequence = write.call_args.args[1]
        self.assertEqual(sequence, b"\033]52;c;" + agmux.base64.b64encode(contents.encode()) + b"\a")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(agmux.Error, "limit"):
                agmux.copy_text("x" * 100001)

    def test_clipboard_inside_tmux_uses_its_buffer_without_sending_pane_keys(self):
        class FakeTmux:
            command = ["/bin/tmux", "-S", "/tmp/fake.sock"]
        with patch.dict(os.environ, {"TMUX": "fake"}, clear=True), \
                patch.object(agmux.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertIn("tmux buffer", agmux.copy_text("selected", FakeTmux()))
        self.assertEqual(run.call_args.args[0][-3:], ["load-buffer", "-w", "-"])
        self.assertEqual(run.call_args.kwargs["input"], "selected")


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
