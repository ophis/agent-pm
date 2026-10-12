import dataclasses
from pathlib import Path
from unittest import mock

from agent_pm.delegate.plan import ConfigError, policy
from agent_pm.session.adapters.base import Access, Spec
from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.codex import CodexAdapter

from new.tests.case import NEW
from new.tests.selection import configured
from new.tests.unit.adapters.codex.support import NATIVE, Base


class PolicyTest(Base):
    def test_selected_policy_is_declarative(self):
        config = {"policy": {"codex": {"flags": ["-s", "workspace-write"], "env": {"K": "v"}}}}
        with mock.patch("agent_pm.delegate.plan.read_config", return_value=config):
            self.assertEqual(policy("codex"), (("-s", "workspace-write"), {"K": "v"}))
            self.assertEqual(policy("fake"), ((), {}))

    def test_invalid_selected_policy_fails_before_launch(self):
        for value in (None, [], {"flags": "-s"}, {"flags": [1]}, {"env": {"K": 1}}, {"unknown": 1}):
            with (
                self.subTest(value=value),
                mock.patch(
                    "agent_pm.delegate.plan.read_config",
                    return_value={"policy": {"codex": value}},
                ),
                self.assertRaises(ConfigError),
            ):
                policy("codex")

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
        first = self.adapter.headless().launch("one", spec, False)
        state = self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        changed = dataclasses.replace(
            spec, flags=("-s", "danger-full-access"), env={"K": "changed"}
        )
        adapter = configured(CodexAdapter, {**self.adapter.config, "env": {"K": "config"}})
        resumed = adapter.headless().launch("two", changed, True)
        self.assertEqual(state.snapshot["flags"], list(flags))
        self.assertEqual(first.env["K"], "saved")
        self.assertEqual(resumed.env["K"], "saved")
        self.assertIn("workspace-write", resumed.argv)
        self.assertNotIn("danger-full-access", resumed.argv)

    def test_spec_accepts_both_environment_sources(self):
        spec = Spec(workdir="/w", access=Access(env={"A": "1"}), env={"B": "2"})
        self.assertEqual((spec.access.env, dict(spec.env)), ({"A": "1"}, {"B": "2"}))

    def test_default_policy_reaches_both_runtime_launches(self):
        for runtime in ("claude", "codex"):
            flags, env = policy(runtime)
            if runtime == "codex":
                adapter = self.adapter
                spec = dataclasses.replace(self.spec, flags=flags, env=env)
                expected = (
                    "-s",
                    "danger-full-access",
                    "-c",
                    'approval_policy="on-request"',
                    "-c",
                    'approvals_reviewer="auto_review"',
                )
            else:
                adapter = configured(ClaudeAdapter, {})
                spec = Spec(workdir=str(self.work), sid=NATIVE, flags=flags, env=env)
                expected = ("--permission-mode", "auto", "--strict-mcp-config")
            launch = adapter.headless().launch("hi", spec, False)
            start = launch.argv.index(expected[0])
            self.assertEqual(launch.argv[start : start + len(expected)], expected)

    def test_adapters_contain_no_role_policy_names(self):
        root = Path(NEW) / "core" / "src" / "agent_pm" / "session" / "adapters"
        forbidden = ("role_policy", "permission-mode", "approval_policy", "approvals_reviewer")
        for path in root.rglob("*.py"):
            for name in forbidden:
                with self.subTest(path=path, name=name):
                    self.assertNotIn(name, path.read_text())

    def test_recovery_helpers_use_driver_selection_without_literal_builders(self):
        root = Path(NEW) / "core" / "src" / "agent_pm" / "session" / "adapters"
        for runtime in ("claude", "codex"):
            source = (root / runtime / "common.py").read_text()
            with self.subTest(runtime=runtime):
                self.assertNotIn("--resume", source)
                self.assertNotIn('"resume"', source)
