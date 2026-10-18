import base64
import io
import json
import runpy
from datetime import UTC, datetime
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

    def test_rejection_diagnostics_keep_existing_strict_gate_and_redact_values(self):
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
            {"type": "error", "message": "secret-error"},
            {"type": "item.completed", "item": {"type": "error", "message": "secret-item"}},
            {"type": "item.updated", "item": {"type": "secret-unknown-kind"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "secret-last"}},
            {"type": "turn.completed"},
        ]
        data = "\n".join(json.dumps(event) for event in events).encode()
        result = self.probe["summarize_turn"](data, 0)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["last_reply_ok"])
        self.assertEqual(result["error_event_count"], 1)
        self.assertEqual(result["rejected_item_type_counts"], {"error": 1, "other": 1})
        self.assertNotIn("secret", json.dumps(result))

    def test_resume_uses_existing_explicit_identity_and_verifies_emitted_identity(self):
        sid = "11111111-2222-4333-8444-555555555555"
        path = f"/codex/sessions/2026/10/18/rollout-2026-10-18T01-49-05-{sid}.jsonl\n"
        for emitted, expected in [(sid, "pass"), ("another-id", "incomplete")]:
            events = [
                {"type": "thread.started", "thread_id": emitted},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
                {"type": "turn.completed"},
            ]
            outputs = [
                b"codex-cli 0.163.0",
                path.encode(),
                "\n".join(map(json.dumps, events)).encode(),
            ]
            responses = [SimpleNamespace(returncode=0, stdout=data, stderr=b"") for data in outputs]
            with mock.patch("subprocess.run", side_effect=responses) as run:
                result = self.probe["resume_turn"]("task647-codex-a-1")
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["same_native_session"], emitted == sid)
            argv = run.call_args.args[0]
            self.assertEqual(argv[-3:], ["resume", sid, "Reply exactly OK. Do not use tools."])
            self.assertIn("read-only", argv)
            self.assertNotIn("--last", argv)
            self.assertNotIn(sid, json.dumps(result))
            self.assertNotIn("another-id", json.dumps(result))

    def test_resume_missing_or_ambiguous_history_never_starts_a_fresh_turn(self):
        for paths in (
            b"",
            b"/codex/sessions/x\n/codex/sessions/y\n",
            b"/elsewhere/rollout.jsonl\n",
        ):
            responses = [
                SimpleNamespace(returncode=0, stdout=b"codex-cli 0.163.0", stderr=b""),
                SimpleNamespace(returncode=0, stdout=paths, stderr=b""),
            ]
            with (
                mock.patch("subprocess.run", side_effect=responses) as run,
                self.assertRaises(ValueError),
            ):
                self.probe["resume_turn"]("task647-codex-a-1")
            self.assertEqual(run.call_count, 2)

    def test_expiry_reports_only_unsigned_time_claims_from_fake_auth(self):
        claims = {"exp": 1800003600, "iat": 1800000000, "secret": "secret-identity"}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        self.auth["tokens"]["access_token"] = f"header.{payload}.signature"
        result = self.probe["access_expiry"](self.auth, datetime.fromtimestamp(1800000100, UTC))
        self.assertEqual(result["remaining_seconds"], 3500)
        self.assertEqual(result["declared_lifetime_seconds"], 3600)
        self.assertEqual(result["signature_status"], "not_verified")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn('signature"', json.dumps(result))
        self.auth["tokens"]["access_token"] = "opaque"
        with self.assertRaises(ValueError):
            self.probe["access_expiry"](self.auth, datetime.now(UTC))

    def test_error_item_categories_match_pinned_templates_without_echoing_substitutions(self):
        examples = [
            (
                "Codex couldn't save diagnostic logs to its local database. Run `codex doctor` for diagnostics.",
                "diagnostic_log_write",
            ),
            (
                "Codex couldn't save diagnostic logs to its local database. Use /feedback with logs included before closing Codex, or run `codex doctor` for diagnostics.",
                "diagnostic_log_write",
            ),
            (
                "Under-development features enabled: private-canary. Under-development features are incomplete and may behave unpredictably. To suppress this warning, set `suppress_unstable_features_warning = true` in /private-canary/config.toml.",
                "under_development_features",
            ),
            (
                "Model metadata for `private-canary` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.",
                "model_metadata_fallback",
            ),
            (
                "Falling back from WebSockets to HTTPS transport. private-canary\nprivate-detail",
                "transport_fallback",
            ),
            ("in-process app-server event stream lagged; dropped 17 events", "event_stream_lag"),
            ("model rerouted: private-canary -> private-target (private-reason)", "model_rerouted"),
        ]
        for message, category in examples:
            self.assertEqual(self.probe["error_item_category"](message), category)
        for unknown in (
            None,
            {"secret": "private-canary"},
            "warning private-canary",
            examples[0][0] + " private-canary",
        ):
            self.assertEqual(self.probe["error_item_category"](unknown), "unknown")

    def test_known_notice_category_does_not_relax_strict_turn_gate(self):
        for message, category in [
            (
                "Codex couldn't save diagnostic logs to its local database. Run `codex doctor` for diagnostics.",
                "diagnostic_log_write",
            ),
            ("private-canary", "unknown"),
            ("Credential masking is inactive because sandboxing is disabled.", "unknown"),
        ]:
            events = [
                {"type": "item.completed", "item": {"type": "error", "message": message}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
                {"type": "turn.completed"},
            ]
            result = self.probe["summarize_turn"]("\n".join(map(json.dumps, events)).encode(), 0)
            self.assertEqual(result["error_item_category_counts"], {category: 1})
            self.assertEqual(result["status"], "incomplete")
            self.assertTrue(result["last_reply_ok"])
            self.assertEqual(result["error_event_count"], 0)
            self.assertEqual(result["rejected_item_type_counts"], {"error": 1})
            self.assertNotIn("private-canary", json.dumps(result))
            self.assertNotIn("Credential masking", json.dumps(result))

    def test_single_resume_keeps_nonzero_strict_result_and_redacts_notice_body(self):
        sid = "11111111-2222-4333-8444-555555555555"
        path = f"/codex/sessions/2026/10/18/rollout-2026-10-18T01-49-05-{sid}.jsonl\n"
        events = [
            {"type": "thread.started", "thread_id": sid},
            {"type": "item.completed", "item": {"type": "error", "message": "private-canary"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
            {"type": "turn.completed"},
        ]
        outputs = [b"codex-cli 0.163.0", path.encode(), "\n".join(map(json.dumps, events)).encode()]
        responses = [
            SimpleNamespace(returncode=0, stdout=data, stderr=b"private-stderr") for data in outputs
        ]
        with (
            mock.patch("sys.argv", ["probe", "--user-run", "resume", "task647-codex-a-1"]),
            mock.patch("subprocess.run", side_effect=responses) as run,
            mock.patch("sys.stdout", new_callable=io.StringIO) as captured,
        ):
            code = self.probe["main"]()
        self.assertEqual(code, 1)
        result = json.loads(captured.getvalue())
        self.assertTrue(result["same_native_session"])
        self.assertEqual(result["error_item_category_counts"], {"unknown": 1})
        self.assertNotIn("private-", captured.getvalue())
        self.assertNotIn(sid, captured.getvalue())
        self.assertEqual(run.call_count, 3)
        self.assertIn("read-only", run.call_args.args[0])
