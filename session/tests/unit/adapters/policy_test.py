"""A caller's policy (Spec.flags and env) reaches each runtime's launch; the adapters name no policy."""

import dataclasses
from pathlib import Path

from agent_pm.session import Access, Spec
from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.codex import CodexAdapter

from session.tests.case import REPO
from session.tests.unit.adapters.codex.support import NATIVE, Base

CODEX = ("-s", "danger-full-access", "-c", 'approval_policy="on-request"')
CLAUDE = ("--permission-mode", "auto", "--strict-mcp-config")


class PolicyTest(Base):
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
        adapter = CodexAdapter({**self.adapter.config, "env": {"K": "config"}})
        resumed = adapter.headless().launch("two", changed, True)
        self.assertEqual(state.snapshot["flags"], list(flags))
        self.assertEqual(first.env["K"], "saved")
        self.assertEqual(resumed.env["K"], "saved")
        self.assertIn("workspace-write", resumed.argv)
        self.assertNotIn("danger-full-access", resumed.argv)

    def test_spec_accepts_both_environment_sources(self):
        spec = Spec(workdir="/w", access=Access(env={"A": "1"}), env={"B": "2"})
        self.assertEqual((spec.access.env, dict(spec.env)), ({"A": "1"}, {"B": "2"}))

    def test_a_policy_reaches_both_runtime_launches(self):
        for flags in (CODEX, CLAUDE):
            if flags is CODEX:
                adapter = self.adapter
                spec = dataclasses.replace(self.spec, flags=flags)
            else:
                adapter = ClaudeAdapter({})
                spec = Spec(workdir=str(self.work), sid=NATIVE, flags=flags)
            launch = adapter.headless().launch("hi", spec, False)
            start = launch.argv.index(flags[0])
            self.assertEqual(launch.argv[start : start + len(flags)], flags)

    def test_adapters_contain_no_role_policy_names(self):
        root = Path(REPO) / "session" / "src" / "agent_pm" / "session" / "adapters"
        forbidden = ("role_policy", "permission-mode", "approval_policy", "approvals_reviewer")
        for path in root.rglob("*.py"):
            for name in forbidden:
                with self.subTest(path=path, name=name):
                    self.assertNotIn(name, path.read_text())

    def test_recovery_helpers_use_driver_selection_without_literal_builders(self):
        root = Path(REPO) / "session" / "src" / "agent_pm" / "session" / "adapters"
        for runtime in ("claude", "codex"):
            source = (root / runtime / "common.py").read_text()
            with self.subTest(runtime=runtime):
                self.assertNotIn("--resume", source)
                self.assertNotIn('"resume"', source)
