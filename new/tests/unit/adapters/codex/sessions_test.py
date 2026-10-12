"""adapters/codex/sessions.py: the recovery snapshot's session env, by the same rule as Access.env."""

from agent_pm.session.adapters import base
from agent_pm.session.adapters.codex.sessions import RecoveryError, validate_snapshot

from new.tests.unit.adapters.codex.support import Base

GOOD = ({}, {"PYTHONPATH": "/a b;c", "_x1": ""})
BAD = (
    {"X;Y": "a"},
    {"A=B": "a"},
    {"1A": "a"},
    {"": "a"},
    {"A B": "a"},
    {"A\n": "a"},
    {1: "a"},
    {"A": 1},
    {"A": None},
    {"A": "b\0"},
    [("A", "b")],
    None,
)


class SnapshotEnvTest(Base):
    def snapshot(self, **fields):
        return {**self.common.prepare(self.spec, False, persist=False).snapshot, **fields}

    def test_keeps_an_env_access_env_accepts(self):
        for env in GOOD:
            with self.subTest(env=env):
                self.assertTrue(base.env_ok(env))
                self.assertEqual(validate_snapshot(self.snapshot(env=env))["env"], env)

    def test_refuses_an_env_access_env_refuses(self):
        for env in BAD:
            with self.subTest(env=env):
                self.assertFalse(base.env_ok(env))
                with self.assertRaises(RecoveryError) as caught:
                    validate_snapshot(self.snapshot(env=env))
                self.assertEqual(str(caught.exception), "invalid Codex recovery snapshot env")
