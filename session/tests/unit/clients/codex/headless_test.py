"""Codex headless launches, native events and usage limits."""

import dataclasses
import json
import sys
from datetime import UTC, datetime

from agent_pm.session.clients.codex import CodexClient
from agent_pm.session.engine.types import (
    Activity,
    ClientBound,
    ConfigError,
    LimitHit,
    Reply,
    Stderr,
    TurnEnd,
)

from session.tests.unit.clients.codex.support import (
    NATIVE,
    PLUGIN_OFF,
    Base,
    committed_headless,
    packaged,
)

CODEX = ("-s", "danger-full-access", "-c", 'approval_policy="on-request"')

HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "context")


class HeadlessTest(Base):
    def run_instance(self):
        run = self.client.headless()
        run.launch(dataclasses.replace(self.spec, prompt="hello"))
        run.commit()
        return run

    def test_native_binding_precedes_event(self):
        run = self.run_instance()
        (event,) = run.parse({"type": "thread.started", "thread_id": NATIVE})
        self.assertEqual(event, ClientBound(NATIVE))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_bind_failure_never_emits_session_bound(self):
        run = self.run_instance()
        self.record().unlink()
        with self.assertRaises(ConfigError):
            list(run.parse({"type": "thread.started", "thread_id": NATIVE}))

    def test_complete_messages_and_final_result_keep_blank_lines(self):
        run = self.client.headless()
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
        run = self.client.headless()
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

    def test_source_usage_and_quota_errors_without_reset_emit_limit(self):
        messages = (
            "You’ve hit your usage limit. Try again later.",
            (
                "You’ve hit your usage limit for GPT-5. Switch to another model now, or try again later."
            ),
            "Your workspace is out of credits. Add credits to continue.",
            (
                "You hit your spend cap set by the owner of your workspace. Ask an owner to increase your spend cap to continue."
            ),
            "Quota exceeded. Check your plan and billing details.",
        )
        for kind in ("error", "turn.failed"):
            for message in messages:
                with self.subTest(kind=kind, message=message):
                    run = self.client.headless()
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
        run.launch(dataclasses.replace(self.spec, prompt="again", resume=True))
        self.assertEqual(list(run.parse(record)), [Stderr(message), LimitHit()])

    def test_a_resume_spec_resumes_its_native_session_with_its_prompt(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        spec = dataclasses.replace(self.spec, prompt="again", resume=True)
        argv = committed_headless(self.client, spec).argv
        start = argv.index("resume")
        self.assertEqual((argv[start + 1], argv[-2:]), (NATIVE, ("--", "again")))

    def test_source_local_reset_formats_become_unix_seconds(self):
        self.client.common.config["env"] = {"TZ": "UTC"}
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
                run = self.client.headless()
                self.assertEqual(
                    list(run.parse({"type": "error", "message": message})),
                    [Stderr(message), LimitHit(expected)],
                )

    def test_ambiguous_invalid_or_unrecognized_reset_remains_unknown(self):
        self.client.common.config["env"] = {"TZ": "America/New_York"}
        for value in (
            "Nov 1st, 2026 1:30 AM",
            "Mar 8th, 2026 2:30 AM",
            "Oct 32nd, 2026 4:30 PM",
            "16:30 UTC",
        ):
            message = f"You’ve hit your usage limit. Try again at {value}."
            with self.subTest(value=value):
                run = self.client.headless()
                self.assertEqual(
                    list(run.parse({"type": "error", "message": message})),
                    [Stderr(message), LimitHit(None)],
                )

    def test_arbitrary_error_is_not_usage_limit(self):
        run = self.client.headless()
        message = "network error mentioning a usage limit"
        self.assertEqual(list(run.parse({"type": "error", "message": message})), [Stderr(message)])


class LaunchPolicy(Base):
    def argv(self, client, spec, *, resume=False):
        return list(
            committed_headless(client, dataclasses.replace(spec, prompt="hi", resume=resume)).argv
        )

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
                first = self.argv(self.client, spec)
                self.assertEqual(first[first.index("-s") :][: len(flags)], list(flags))
                self.common.bind(str(self.work), spec.sid, NATIVE)
                self.rollout()
                changed = dataclasses.replace(spec, flags=("-s", current), env={"K": "changed"})
                second = self.argv(self.client, changed, resume=True)
                self.assertEqual(second[second.index("-s") :][: len(flags)], list(flags))
                self.assertEqual(
                    self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(flags)
                )

    def test_separator_tail_follows_generated_options_on_fresh_and_resume(self):
        mode = "headless"
        with self.subTest(mode=mode):
            supplied = ("--enable", "web", "--", "literal", "--last")
            spec = dataclasses.replace(self.spec, sid=f"separator-{mode}", flags=supplied)
            first = self.argv(self.client, spec)
            self.common.bind(str(self.work), spec.sid, NATIVE)
            self.rollout()
            changed = dataclasses.replace(spec, flags=("--", "changed"))
            second = self.argv(self.client, changed, resume=True)
            for argv in (first, second):
                boundary = argv.index("--")
                self.assertEqual(argv[boundary:], ["--", "literal", "--last", "hi"])
                self.assertIn("gpt-test", argv[:boundary])
                self.assertIn(str(self.extra), argv[:boundary])
                self.assertIn("project_doc_max_bytes=0", argv[:boundary])
            self.assertEqual(
                self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(supplied)
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
        first = committed_headless(self.client, dataclasses.replace(spec, prompt="one"))
        state = self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        changed = dataclasses.replace(
            spec, flags=("-s", "danger-full-access"), env={"K": "changed"}
        )
        client = CodexClient({**self.client.config, "env": {"K": "config"}})
        resumed = committed_headless(
            client, dataclasses.replace(changed, prompt="two", resume=True)
        )
        self.assertEqual(state.snapshot.flags, tuple(flags))
        self.assertEqual(first.env["K"], "saved")
        self.assertEqual(resumed.env["K"], "saved")
        self.assertIn("workspace-write", resumed.argv)
        self.assertNotIn("danger-full-access", resumed.argv)


class SuppliedPolicy(Base):
    def test_a_policy_reaches_both_client_launches(self):
        flags = CODEX
        client = self.client
        spec = dataclasses.replace(self.spec, flags=flags)
        launch = committed_headless(client, dataclasses.replace(spec, prompt="hi"))
        start = launch.argv.index(flags[0])
        self.assertEqual(launch.argv[start : start + len(flags)], flags)


class ConfiguredFlags(Base):
    OWN = ("-c", "plugins.x@m.enabled=false")

    def test_the_configs_flags_precede_the_specs_and_resume_keeps_them(self):
        client = CodexClient({**self.client.config, "flags": list(self.OWN)})
        spec = dataclasses.replace(self.spec, flags=CODEX)
        first = committed_headless(client, dataclasses.replace(spec, prompt="one"))
        start = first.argv.index(self.OWN[0])
        self.assertEqual(first.argv[start : start + 6], (*self.OWN, *CODEX))
        state = self.common.bind(str(self.work), spec.sid, NATIVE)
        self.assertEqual(state.snapshot.flags, (*self.OWN, *CODEX))
        self.rollout()
        resumed = committed_headless(
            self.client, dataclasses.replace(spec, prompt="two", resume=True)
        )
        self.assertIn(self.OWN[1], resumed.argv)

    def test_the_packaged_config_turns_the_agent_pm_plugin_off(self):
        spec = dataclasses.replace(self.spec, flags=CODEX, prompt="hi")
        argv = committed_headless(packaged(), spec).argv
        start = argv.index(PLUGIN_OFF[1]) - 1
        self.assertEqual(argv[start : start + 2], PLUGIN_OFF)

    def test_bad_configured_flags_are_refused(self):
        for flags, message in (
            ("-c", "[clients.codex].flags must be a list of strings"),
            ([1], "[clients.codex].flags must be a list of strings"),
            (["-C", "/x"], "-C /x: the Codex client sets hooks, session and cwd"),
            (["-c", "hooks.x=1"], "-c hooks.x=1: the Codex client sets hooks, session and cwd"),
        ):
            with self.subTest(flags=flags), self.assertRaises(ConfigError) as caught:
                CodexClient({**self.client.config, "flags": flags})
            self.assertEqual(str(caught.exception), message)
