"""Codex recovery snapshots validate session environment by the Access.env rule."""

import json

from agent_pm.session import Access, ConfigError
from agent_pm.session.adapters.codex.sessions import (
    RecoveryError,
    read_metadata,
    snapshot_data,
    validate_snapshot,
)
from agent_pm.session.adapters.codex.types import NativeMetadata

from session.tests.unit.adapters.codex.support import CHILD, NATIVE, Base

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
            **snapshot_data(self.common.prepare(self.spec, persist=False, resume=False).snapshot),
            **fields,
        }

    def test_keeps_an_env_access_env_accepts(self):
        for env in GOOD:
            with self.subTest(env=env):
                self.assertEqual(Access(env=env).env, env)
                self.assertEqual(validate_snapshot(self.snapshot(env=env)).env, env)

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


class CodecTest(Base):
    def test_snapshot_codec_detaches_both_sides_and_keeps_the_persisted_schema(self):
        state = self.prepare()
        raw = snapshot_data(state.snapshot)
        decoded = validate_snapshot(raw)
        raw["dirs"].append("/changed")
        raw["commands"].append("changed")
        raw["flags"].append("--changed")
        raw["env"]["CHANGED"] = "yes"
        self.assertEqual(decoded, state.snapshot)
        encoded = snapshot_data(decoded)
        encoded["dirs"].append("/changed-again")
        encoded["env"]["CHANGED"] = "again"
        self.assertEqual(decoded, state.snapshot)
        persisted = json.loads(self.record().read_text())
        self.assertEqual(persisted["version"], 1)
        self.assertEqual(persisted["snapshot"], snapshot_data(state.snapshot))
        self.assertEqual(persisted["writable_roots"], list(state.writable_roots))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid), state)

    def test_native_parent_extraction_preserves_precedence_and_forgiving_source_formats(self):
        path = self.rollout()
        source = {"subagent": {"thread_spawn": {"parent_thread_id": CHILD, "depth": 1}}}
        for parent, origin, expected in (
            (NATIVE, source, NATIVE),
            (None, source, CHILD),
            ("invalid", source, None),
            ({}, source, None),
            (None, "exec", None),
            (None, {"subagent": "invalid"}, None),
            (None, {"subagent": {"thread_spawn": []}}, None),
            (None, {"subagent": {"thread_spawn": {"parent_thread_id": {}}}}, None),
        ):
            payload = {
                "id": NATIVE,
                "cwd": str(self.cwd),
                "parent_thread_id": parent,
                "source": origin,
                "other": {"mutable": []},
            }
            path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
            with self.subTest(parent=parent, origin=origin):
                metadata = read_metadata(str(path)).metadata
                self.assertEqual(metadata, NativeMetadata(NATIVE, str(self.cwd), expected))
