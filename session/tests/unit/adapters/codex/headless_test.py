"""Codex headless launches, native events and usage limits."""

import dataclasses
import json
import sys
from datetime import UTC, datetime

from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.engine.types import (
    Activity,
    ConfigError,
    LimitHit,
    Reply,
    SessionBound,
    Stderr,
    TurnEnd,
)

from session.tests.unit.adapters.codex.support import NATIVE, Base

CODEX = ("-s", "danger-full-access", "-c", 'approval_policy="on-request"')

HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "context")


class HeadlessTest(Base):
    def run_instance(self):
        run = self.adapter.headless()
        run.launch("hello", self.spec, resume=False)
        return run

    def test_native_binding_precedes_event(self):
        run = self.run_instance()
        (event,) = run.parse({"type": "thread.started", "thread_id": NATIVE})
        self.assertEqual(event, SessionBound(NATIVE))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_bind_failure_never_emits_session_bound(self):
        run = self.run_instance()
        self.record().unlink()
        with self.assertRaises(ConfigError):
            list(run.parse({"type": "thread.started", "thread_id": NATIVE}))

    def test_complete_messages_and_final_result_keep_blank_lines(self):
        run = self.adapter.headless()
        texts = ["one\n\nsecond line\n", "[agent-pm-progress:start] x", "three"]
        lines = [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
            for text in texts
        ]
        lines.append(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
        self.assertEqual(
            list(run.events(lines)), [*(Reply(text) for text in texts), TurnEnd(texts[-1])]
        )

    def test_activity_and_invalid_record_diagnostics(self):
        run = self.adapter.headless()
        self.assertEqual(
            list(run.events(["not json", "[]"])), [Stderr("codex: invalid JSON event")]
        )
        self.assertEqual(
            list(run.parse({"type": "thread.started", "thread_id": "not-a-uuid"})),
            [Stderr("codex: invalid thread.started ID")],
        )
        self.assertEqual(
            list(run.parse({"type": "item.started", "item": {"type": "command_execution"}})),
            [Activity("item.started")],
        )

    def test_source_usage_errors_without_reset_emit_limit(self):
        messages = (
            "You’ve hit your usage limit. Try again later.",
            (
                "You’ve hit your usage limit for GPT-5. Switch to another model now, or try again later."
            ),
            "Your workspace is out of credits. Add credits to continue.",
            (
                "You hit your spend cap set by the owner of your workspace. Ask an owner to increase your spend cap to continue."
            ),
        )
        for kind in ("error", "turn.failed"):
            for message in messages:
                with self.subTest(kind=kind, message=message):
                    run = self.adapter.headless()
                    record = (
                        {"type": kind, "message": message}
                        if kind == "error"
                        else {"type": kind, "error": {"message": message}}
                    )
                    self.assertEqual(list(run.parse(record)), [Stderr(message), LimitHit(None)])

    def test_paired_and_repeated_limit_errors_keep_stderr_once_per_run(self):
        message = "Quota exceeded. Check your plan and billing details."
        records = (
            {"type": "error", "message": message},
            {"type": "turn.failed", "error": {"message": message}},
            {"type": "error", "message": message},
        )
        run = self.run_instance()
        self.assertEqual(
            list(run.events(map(json.dumps, records))),
            [Stderr(message), LimitHit(), Stderr(message), Stderr(message)],
        )

    def test_non_limit_error_does_not_suppress_later_limit(self):
        run = self.run_instance()
        message = "Quota exceeded. Check your plan and billing details."
        self.assertEqual(
            list(run.parse({"type": "error", "message": "network error"})),
            [Stderr("network error")],
        )
        self.assertEqual(
            list(run.parse({"type": "turn.failed", "error": {"message": message}})),
            [Stderr(message), LimitHit()],
        )

    def test_fresh_launch_resets_limit_guard(self):
        run = self.run_instance()
        message = "Quota exceeded. Check your plan and billing details."
        record = {"type": "error", "message": message}
        self.assertEqual(list(run.parse(record)), [Stderr(message), LimitHit()])
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        run.launch("again", self.spec, resume=True)
        self.assertEqual(list(run.parse(record)), [Stderr(message), LimitHit()])

    def test_source_quota_error_emits_limit(self):
        run = self.adapter.headless()
        message = "Quota exceeded. Check your plan and billing details."
        self.assertEqual(
            list(run.parse({"type": "error", "message": message})),
            [Stderr(message), LimitHit(None)],
        )

    def test_source_quota_failed_turn_emits_limit(self):
        run = self.adapter.headless()
        message = "Quota exceeded. Check your plan and billing details."
        self.assertEqual(
            list(run.parse({"type": "turn.failed", "error": {"message": message}})),
            [Stderr(message), LimitHit(None)],
        )

    def test_source_local_reset_formats_become_unix_seconds(self):
        self.adapter.common.config["env"] = {"TZ": "UTC"}
        today = datetime.now(UTC)
        expected_today = today.replace(hour=16, minute=30, second=0, microsecond=0).timestamp()
        cases = (
            ("You’ve hit your usage limit. Try again at 4:30 PM.", expected_today),
            (
                (
                    "You’ve hit your usage limit. Upgrade to Pro, or try again at Oct 12th, 2026 4:30 PM."
                ),
                datetime(2026, 10, 12, 16, 30, tzinfo=UTC).timestamp(),
            ),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                run = self.adapter.headless()
                self.assertEqual(
                    list(run.parse({"type": "error", "message": message})),
                    [Stderr(message), LimitHit(expected)],
                )

    def test_ambiguous_invalid_or_unrecognized_reset_remains_unknown(self):
        self.adapter.common.config["env"] = {"TZ": "America/New_York"}
        for value in (
            "Nov 1st, 2026 1:30 AM",
            "Mar 8th, 2026 2:30 AM",
            "Oct 32nd, 2026 4:30 PM",
            "16:30 UTC",
        ):
            message = f"You’ve hit your usage limit. Try again at {value}."
            with self.subTest(value=value):
                run = self.adapter.headless()
                self.assertEqual(
                    list(run.parse({"type": "error", "message": message})),
                    [Stderr(message), LimitHit(None)],
                )

    def test_arbitrary_error_is_not_usage_limit(self):
        run = self.adapter.headless()
        message = "network error mentioning a usage limit"
        self.assertEqual(list(run.parse({"type": "error", "message": message})), [Stderr(message)])


class LaunchPolicy(Base):
    def argv(self, adapter, spec, *, resume=False):
        return list(adapter.headless().launch("hi", spec, resume=resume).argv)

    def test_supplied_policy_and_saved_replay_in_both_modes(self):
        mode = "headless"
        for saved, current in (
            ("workspace-write", "danger-full-access"),
            ("danger-full-access", "workspace-write"),
        ):
            with self.subTest(mode=mode, saved=saved):
                flags = (
                    "-s",
                    saved,
                    "-c",
                    'approval_policy="on-request"',
                    "-c",
                    'approvals_reviewer="auto_review"',
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                )
                spec = dataclasses.replace(
                    self.spec, sid=f"{mode}-{saved}", flags=flags, env={"K": "saved"}
                )
                first = self.argv(self.adapter, spec)
                self.assertEqual(first[first.index("-s") :][: len(flags)], list(flags))
                self.common.bind(str(self.work), spec.sid, NATIVE)
                self.rollout()
                changed = dataclasses.replace(spec, flags=("-s", current), env={"K": "changed"})
                second = self.argv(self.adapter, changed, resume=True)
                self.assertEqual(second[second.index("-s") :][: len(flags)], list(flags))
                self.assertEqual(
                    self.common.load(str(self.work), spec.sid).snapshot["flags"], list(flags)
                )

    def test_separator_tail_follows_generated_options_on_fresh_and_resume(self):
        mode = "headless"
        with self.subTest(mode=mode):
            supplied = ("--enable", "web", "--", "literal", "--last")
            spec = dataclasses.replace(self.spec, sid=f"separator-{mode}", flags=supplied)
            first = self.argv(self.adapter, spec)
            self.common.bind(str(self.work), spec.sid, NATIVE)
            self.rollout()
            changed = dataclasses.replace(spec, flags=("--", "changed"))
            second = self.argv(self.adapter, changed, resume=True)
            for argv in (first, second):
                boundary = argv.index("--")
                self.assertEqual(argv[boundary:], ["--", "literal", "--last", "hi"])
                self.assertIn("gpt-test", argv[:boundary])
                self.assertIn(str(self.extra), argv[:boundary])
                self.assertIn("project_doc_max_bytes=0", argv[:boundary])
            self.assertEqual(
                self.common.load(str(self.work), spec.sid).snapshot["flags"], list(supplied)
            )


class PolicyReplay(Base):
    def test_snapshot_replays_supplied_flags_and_environment(self):
        flags = (
            "-s",
            "workspace-write",
            "-c",
            'approval_policy="on-request"',
            "--add-dir",
            str(self.extra),
        )
        spec = dataclasses.replace(self.spec, flags=flags, env={"K": "saved"})
        first = self.adapter.headless().launch("one", spec, resume=False)
        state = self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        changed = dataclasses.replace(
            spec, flags=("-s", "danger-full-access"), env={"K": "changed"}
        )
        adapter = CodexAdapter({**self.adapter.config, "env": {"K": "config"}})
        resumed = adapter.headless().launch("two", changed, resume=True)
        self.assertEqual(state.snapshot["flags"], list(flags))
        self.assertEqual(first.env["K"], "saved")
        self.assertEqual(resumed.env["K"], "saved")
        self.assertIn("workspace-write", resumed.argv)
        self.assertNotIn("danger-full-access", resumed.argv)


class SuppliedPolicy(Base):
    def test_a_policy_reaches_both_runtime_launches(self):
        flags = CODEX
        adapter = self.adapter
        spec = dataclasses.replace(self.spec, flags=flags)
        launch = adapter.headless().launch("hi", spec, resume=False)
        start = launch.argv.index(flags[0])
        self.assertEqual(launch.argv[start : start + len(flags)], flags)
