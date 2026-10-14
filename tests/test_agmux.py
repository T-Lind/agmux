import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "agmux.py"
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

    def test_trim_chrome_leaves_shell_alone(self):
        lines = ["$ ls -la", "total 0", "$ echo hi", "", ""]
        self.assertEqual(agmux.trim_chrome("shell", lines), ["$ ls -la", "total 0", "$ echo hi"])
        self.assertEqual(agmux.trim_chrome(None, ["plain", "text"]), ["plain", "text"])

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
            with patch.object(agmux, "CODEX_HOME", home):
                limits = agmux.find_codex_rate_limits()
                self.assertEqual(limits["plan_type"], "plus")
                self.assertIn("Codex", agmux.usage_inline(limits))
                self.assertIn("42%", agmux.usage_inline(limits))
                report = "\n".join(text for text, _ in agmux.usage_report(limits=limits))
                self.assertIn("5h", report)
                self.assertIn("credits: 3", report)

    def test_missing_usage_is_graceful(self):
        with tempfile.TemporaryDirectory(prefix="agmux-usage-") as directory:
            with patch.object(agmux, "CODEX_HOME", Path(directory)):
                self.assertIsNone(agmux.find_codex_rate_limits())
                self.assertEqual(agmux.usage_inline(None), "")
                self.assertTrue(agmux.usage_report(limits=None))


class Notifications(unittest.TestCase):
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
        # os.execv is patched so the test process is preserved; tmux is absent
        # from the environment, so status recording is a safe no-op.
        with patch.object(agmux.os, "execv") as execv, \
                patch.object(agmux.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = None
            popen.return_value.returncode = 0
            agmux.launch_agent("codex", "/bin/true", "/bin/bash", "s1", ["--flag"], False)
        execv.assert_called_once_with("/bin/bash", ["/bin/bash", "-l"])
        popen.assert_called_once_with(["/bin/true", "--flag"])


if __name__ == "__main__":
    unittest.main()
