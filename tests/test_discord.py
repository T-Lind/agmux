import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("AGMUX_CONFIG", os.devnull)
import agmux
import agmux_discord as discord


class DiscordAlerts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = patch.dict(os.environ, {"XDG_CONFIG_HOME": directory.name})
        config.start()
        self.addCleanup(config.stop)
        self.path = "/api/webhooks/123/" + "test-only"
        self.url = "https://discord.com" + self.path

    def test_private_setup_rotation_and_strict_destination(self):
        discord.save_webhook(self.url)
        self.assertEqual(discord.webhook_file().stat().st_mode & 0o777, 0o600)
        self.assertEqual(discord.load_webhook(), self.path)
        for invalid in (self.url.replace("discord.com", "discord.com.example"),
                        self.url.replace("https:", "http:"), self.url + "?thread_id=1"):
            with self.assertRaises(ValueError):
                discord.save_webhook(invalid)
            self.assertEqual(discord.load_webhook(), self.path)
        discord.save_webhook(self.url + "2")
        self.assertEqual(discord.load_webhook(), self.path + "2")
        discord.webhook_file().chmod(0o644)
        with self.assertRaises(ValueError):
            discord.load_webhook()

    def test_urgency_and_metadata(self):
        for title, code, quiet in (("Codex needs input", -1, False),
                                   ("Codex turn finished", 0, True),
                                   ("Codex closed", 0, True), ("Claude Code finished", 3, False)):
            payload = discord.message(title, "@everyone " + self.url, code)
            self.assertEqual(bool(payload["flags"] & discord.SUPPRESS_NOTIFICATIONS), quiet)
            self.assertTrue(payload["flags"] & discord.SUPPRESS_EMBEDS)
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertNotIn(self.path, payload["content"])
            if code > 0:
                self.assertIn(f"exit {code}", payload["content"])

    def test_cli_hidden_setup_and_acknowledged_post(self):
        with patch("sys.stdin.isatty", return_value=True), \
                patch.object(discord.getpass, "getpass", return_value=self.url), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(agmux.main(["discord", "--setup"]), 0)
        self.assertNotIn(self.path, output.getvalue())
        with patch.object(discord, "HTTPSConnection") as connect:
            conn = connect.return_value
            conn.getresponse.return_value.status = 200
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(agmux.main(["discord", "--test"]), 0)
            connect.assert_called_once_with("discord.com", timeout=6)
            method, path, body, headers = conn.request.call_args.args
            self.assertEqual((method, path), ("POST", self.path + "?wait=true"))
            self.assertEqual(json.loads(body)["content"], "agmux Discord alerts connected")
            self.assertEqual(headers["Content-Type"], "application/json")

    def test_retries_only_bounded_explicit_rate_limits_and_redacts_errors(self):
        discord.save_webhook(self.url)
        with patch.object(discord, "HTTPSConnection") as connect, patch.object(discord.time, "sleep") as sleep:
            conn = connect.return_value
            limited = Mock(status=429)
            limited.read.return_value = b'{"retry_after": 0.25}'
            conn.getresponse.side_effect = [limited, Mock(status=200)]
            discord.deliver(self.path, {})
            sleep.assert_called_once_with(0.25)
            self.assertEqual(conn.close.call_count, 2)
            for response in (Mock(status=302), Mock(status=404), limited):
                limited.read.return_value = b'{"retry_after": 90}'
                conn.getresponse.side_effect = [response]
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(agmux.main(["discord", "--test"]), 1)
                self.assertNotIn(self.path, output.getvalue())

    def test_notification_hook_excludes_prompt_body_and_uses_installed_command(self):
        custom = 'agmux discord -- "{title}" "{session}" "{exit_code}"'
        with patch.dict(os.environ, {"AGMUX_NOTIFY_CMD": custom}), patch.object(agmux.subprocess, "Popen") as spawn:
            agmux.send_blocked_notification("Codex", "-project", "private question")
            args = spawn.call_args.args[0]
            self.assertEqual(args[1:], [str(agmux.SELF), "discord", "--", "Codex needs input",
                                        "-project", "-1"])
            with patch.object(discord, "deliver") as send, patch.object(discord, "load_webhook"):
                self.assertEqual(agmux.main(args[2:]), 0)
                self.assertEqual(send.call_args.args[1]["content"], "Codex needs input · -project")

    def test_existing_sessions_are_watched_without_replaying_past_completions(self):
        row = dict(id="$1", name="work", created=123, agent="codex", state="idle",
                   status="running", finished=0, done_turn="old", exit_code="")
        new_session = dict(row, id="$2", state="blocked", prompt="Ignore this new launch")
        blocked = dict(row, state="blocked", prompt="Which branch?")
        complete = dict(row, done_turn="new")
        closed = dict(complete, finished=456, exit_code="0", status="exited")
        tmux = Mock()
        tmux.session_details.side_effect = [
            [row], [blocked, new_session], [blocked], [complete], [complete], [closed], [new_session]]
        with patch.object(agmux.time, "sleep"), patch.object(agmux, "_run_notification") as send, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agmux.watch_command(tmux, 1, existing=True), 0)
        self.assertEqual([call.args[0] for call in send.call_args_list],
                         ["Codex needs input", "Codex turn finished", "Codex closed"])
