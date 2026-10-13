"""Supplied policy and exact recovery replay in both Modes."""

import dataclasses
import json
import sys
from unittest import mock

from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.adapters.types import ConfigError
from agent_pm.session.engine.types import SessionRequest

from session.tests.unit.adapters.codex.support import NATIVE, Base

HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "context")


class SandboxTest(Base):
    def argv(self, adapter, mode, spec, *, resume=False):
        if mode == "headless":
            return list(adapter.headless().launch("hi", SessionRequest(spec, resumed=resume)).argv)
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = adapter.tui().launch("hi", SessionRequest(spec, resumed=resume), HOOK)
        return json.loads(launch.argv[-1])["argv"]

    def test_supplied_policy_and_saved_replay_in_both_modes(self):
        for mode in ("headless", "tui"):
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
                    first = self.argv(self.adapter, mode, spec)
                    self.assertEqual(first[first.index("-s") :][: len(flags)], list(flags))
                    self.common.bind(str(self.work), spec.sid, NATIVE)
                    self.rollout()
                    changed = dataclasses.replace(spec, flags=("-s", current), env={"K": "changed"})
                    second = self.argv(self.adapter, mode, changed, resume=True)
                    self.assertEqual(second[second.index("-s") :][: len(flags)], list(flags))
                    self.assertEqual(
                        self.common.load(str(self.work), spec.sid).snapshot["flags"], list(flags)
                    )

    def test_adapter_has_no_policy_configuration(self):
        for key in ("sandbox", "shell_network"):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, "unknown key"):
                CodexAdapter(self.adapter.config | {key: "workspace-write"})

    def test_invalid_saved_flags_fail_closed(self):
        state = self.prepare()
        for value in (None, True, 0, {}, "-s", [1], ["bad\0"]):
            self.edit_record(snapshot=state.snapshot | {"flags": value})
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "snapshot flags"):
                self.common.load(str(self.work), self.spec.sid)

    def test_separator_tail_follows_generated_options_on_fresh_and_resume(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                supplied = ("--enable", "web", "--", "literal", "--last")
                spec = dataclasses.replace(self.spec, sid=f"separator-{mode}", flags=supplied)
                first = self.argv(self.adapter, mode, spec)
                self.common.bind(str(self.work), spec.sid, NATIVE)
                self.rollout()
                changed = dataclasses.replace(spec, flags=("--", "changed"))
                second = self.argv(self.adapter, mode, changed, resume=True)
                for argv in (first, second):
                    boundary = argv.index("--")
                    self.assertEqual(argv[boundary:], ["--", "literal", "--last", "hi"])
                    self.assertIn("gpt-test", argv[:boundary])
                    self.assertIn(str(self.extra), argv[:boundary])
                    self.assertIn("project_doc_max_bytes=0", argv[:boundary])
                    if mode == "tui":
                        self.assertEqual(
                            sum(word.startswith("hooks.") for word in argv[:boundary]), 6
                        )
                self.assertEqual(
                    self.common.load(str(self.work), spec.sid).snapshot["flags"], list(supplied)
                )
