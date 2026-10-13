"""Codex recovery snapshots validate session environment by the Access.env rule."""

from agent_pm.session import Access, ConfigError
from agent_pm.session.adapters.codex.sessions import RecoveryError, validate_snapshot

from session.tests.unit.adapters.codex.support import Base

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
        return {
            **self.common.prepare(self.spec, persist=False, resume=False).snapshot,
            **fields,
        }

    def test_keeps_an_env_access_env_accepts(self):
        for env in GOOD:
            with self.subTest(env=env):
                self.assertEqual(Access(env=env).env, env)
                self.assertEqual(validate_snapshot(self.snapshot(env=env))["env"], env)

    def test_refuses_an_env_access_env_refuses(self):
        for env in BAD:
            with self.subTest(env=env):
                with self.assertRaises(ConfigError):
                    Access(env=env)
                with self.assertRaises(RecoveryError) as caught:
                    validate_snapshot(self.snapshot(env=env))
                self.assertEqual(str(caught.exception), "invalid Codex recovery snapshot env")


class StoreRootTest(Base):
    def test_nonpersisted_prepare_rejects_a_symlinked_default_store(self):
        store = self.home / ".agent-pm" / "core-new-sessions" / "codex"
        target = self.root / "store-target"
        store.parent.mkdir(parents=True, mode=0o700)
        target.mkdir(mode=0o700)
        store.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            self.common.prepare(self.spec, persist=False)
        self.assertEqual(list(target.iterdir()), [])
