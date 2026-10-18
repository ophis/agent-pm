import io
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.case import REPO, Case


class CodexACarrier(Case):
    def setUp(self):
        self.probe = runpy.run_path(str(Path(REPO, "scripts/docker_codex_a_probe.py")))
        self.auth = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": "synthetic-id",
                "access_token": "synthetic-access",
                "refresh_token": "refresh-only-canary",
                "account_id": "synthetic-account",
                "unexpected": "refresh-only-canary",
            },
            "last_refresh": "unchanged-source-date",
            "unexpected": "refresh-only-canary",
        }

    def test_carriers_drop_refresh_values_and_preserve_source(self):
        before = json.dumps(self.auth)
        for variant in ("missing-refresh", "empty-refresh"):
            carrier = self.probe["make_carrier"](self.auth, variant)
            self.assertNotIn("refresh-only-canary", json.dumps(carrier))
            self.assertEqual(carrier["tokens"]["id_token"], "synthetic-id")
            self.assertEqual(carrier["tokens"]["account_id"], "synthetic-account")
            self.assertEqual(carrier["last_refresh"], "unchanged-source-date")
            self.assertNotIn("unexpected", carrier)
            if variant == "missing-refresh":
                self.assertNotIn("refresh_token", carrier["tokens"])
            else:
                self.assertEqual(carrier["tokens"]["refresh_token"], "")
        self.assertEqual(json.dumps(self.auth), before)

    def test_invalid_identity_and_refresh_embedded_in_access_rejected(self):
        self.auth["tokens"]["access_token"] = "refresh-only-canary"
        with self.assertRaises(ValueError):
            self.probe["make_carrier"](self.auth, "empty-refresh")
        self.auth["auth_mode"] = "apikey"
        with self.assertRaises(ValueError):
            self.probe["make_carrier"](self.auth, "missing-refresh")

    def test_escaped_refresh_value_is_also_rejected(self):
        self.auth["tokens"]["refresh_token"] = 'quoted-"-refresh'
        self.auth["tokens"]["access_token"] = 'prefix-quoted-"-refresh-suffix'
        with self.assertRaises(ValueError):
            self.probe["make_carrier"](self.auth, "empty-refresh")

    def test_private_reader_uses_only_fake_file_and_refuses_links_or_public_mode(self):
        root = Path(self.home, "broker")
        root.mkdir(mode=0o700)
        auth = root / "auth.json"
        auth.write_text(json.dumps(self.auth))
        auth.chmod(0o600)
        self.assertEqual(self.probe["read_private_auth"](auth), self.auth)
        auth.chmod(0o644)
        with self.assertRaises(ValueError):
            self.probe["read_private_auth"](auth)
        link = root / "link"
        link.symlink_to(auth)
        with self.assertRaises(OSError):
            self.probe["read_private_auth"](link)

    def test_injection_uses_stdin_not_argv_and_redacts_child_error(self):
        fake = SimpleNamespace(returncode=1, stdout=b"secret-out", stderr=b"secret-error")
        with mock.patch("subprocess.run", return_value=fake) as run:
            result = self.probe["inject"]("task647-codex-a-1", self.auth, "empty-refresh")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["docker", "exec", "-i", "task647-codex-a-1"])
        self.assertNotIn("synthetic-access", " ".join(argv))
        self.assertNotIn("refresh-only-canary", run.call_args.kwargs["input"].decode())
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["exit_code"], 1)

    def test_turn_requires_completed_ok_without_error_or_tool_events(self):
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
            {"type": "turn.completed"},
        ]

        def output(rows):
            return "\n".join(json.dumps(row) for row in rows).encode()

        summary = self.probe["summarize_turn"]
        self.assertEqual(summary(output(events), 0)["status"], "pass")
        self.assertEqual(summary(output(events), 1)["completed_turn_count"], 1)
        self.assertIsNone(summary(b"not-json", 1)["completed_turn_count"])
        for rows, code in [
            (events, 1),
            (events[:1], 0),
            (events + [{"type": "error", "message": "secret-error"}], 0),
            (events + [{"type": "item.started", "item": {"type": "command_execution"}}], 0),
        ]:
            result = summary(output(rows), code)
            self.assertEqual(result["status"], "incomplete")
            self.assertNotIn("secret-error", json.dumps(result))

    def test_plain_exec_keeps_read_only_sandbox_and_fixed_containers(self):
        argv = self.probe["exec_command"]("task647-codex-a-2")
        self.assertEqual(argv[:5], ["docker", "exec", "task647-codex-a-2", "codex", "-c"])
        self.assertIn("read-only", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        with self.assertRaises(ValueError):
            self.probe["exec_command"]("another-container")

    def test_main_requires_explicit_user_flag_before_reading_credentials(self):
        with (
            mock.patch("sys.argv", ["probe", "inject", "task647-codex-a-1", "empty-refresh"]),
            mock.patch("os.open") as opened,
            self.assertRaises(SystemExit) as raised,
        ):
            self.probe["main"]()
        self.assertEqual(raised.exception.code, 2)
        opened.assert_not_called()

    def test_version_mismatch_stops_injection_before_reading_credentials(self):
        fake = SimpleNamespace(returncode=0, stdout=b"codex-cli 0.162.0", stderr=b"")
        with (
            mock.patch(
                "sys.argv", ["probe", "--user-run", "inject", "task647-codex-a-1", "empty-refresh"]
            ),
            mock.patch("subprocess.run", return_value=fake),
            mock.patch("os.open") as opened,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            code = self.probe["main"]()
        self.assertEqual(code, 1)
        opened.assert_not_called()

    def test_pair_needs_actual_time_overlap(self):
        rows = [
            {
                "status": "pass",
                "started_at": "2026-10-18T01:00:00Z",
                "ended_at": "2026-10-18T01:00:04Z",
            },
            {
                "status": "pass",
                "started_at": "2026-10-18T01:00:05Z",
                "ended_at": "2026-10-18T01:00:08Z",
            },
        ]
        self.assertEqual(self.probe["summarize_pair"](rows)["status"], "incomplete")
        rows[1]["started_at"] = "2026-10-18T01:00:03Z"
        self.assertEqual(self.probe["summarize_pair"](rows)["status"], "pass")
