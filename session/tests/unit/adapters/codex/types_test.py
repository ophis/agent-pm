"""Codex typed recovery state detaches mutable inputs and rejects mutable nested values."""

from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

from agent_pm.session.adapters.codex.types import (
    NativeMetadata,
    SessionFile,
    SessionState,
    Snapshot,
)

from session.tests.case import Case


class ImmutableStateTest(Case):
    def test_direct_construction_detaches_collection_and_environment_aliases(self):
        dirs, commands, flags, roots, env = (
            ["/extra"],
            ["git status"],
            ["--verbose"],
            ["/cwd"],
            {"K": "v"},
        )
        snapshot = Snapshot(
            cwd="/cwd",
            project=False,
            dirs=dirs,
            commands=commands,
            model=None,
            effort=None,
            codex_home="/home",
            flags=flags,
            env=MappingProxyType(env),
        )
        state = SessionState("/work", "caller", snapshot, roots)
        dirs.append("/later")
        commands.append("git diff")
        flags.append("--later")
        roots.append("/later")
        env["K"] = "later"
        self.assertEqual(snapshot.dirs, ("/extra",))
        self.assertEqual(snapshot.commands, ("git status",))
        self.assertEqual(snapshot.flags, ("--verbose",))
        self.assertEqual(snapshot.env, {"K": "v"})
        self.assertEqual(state.writable_roots, ("/cwd",))
        with self.assertRaises(TypeError):
            snapshot.env["K"] = "changed"
        with self.assertRaises(FrozenInstanceError):
            snapshot.cwd = "/changed"
        with self.assertRaises(FrozenInstanceError):
            state.sid = "changed"

    def test_direct_construction_rejects_mutable_nested_values(self):
        snapshot = Snapshot(
            cwd="/cwd",
            project=False,
            dirs=(),
            commands=(),
            model=None,
            effort=None,
            codex_home="/home",
            flags=(),
            env={},
        )
        for fields in (
            {"cwd": []},
            {"project": {}},
            {"dirs": [{}]},
            {"commands": [[]]},
            {"flags": [{}]},
            {"env": {"K": {}}},
            {"model": []},
            {"effort": {}},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(snapshot, **fields)
        with self.assertRaises(ValueError):
            SessionState("/work", "caller", snapshot, [{}])

    def test_native_metadata_has_only_immutable_typed_fields(self):
        metadata = NativeMetadata("native", "/cwd", "parent")
        record = SessionFile("/session.jsonl", 10.0, metadata)
        with self.assertRaises(FrozenInstanceError):
            record.metadata.parent_thread_id = "changed"
        for values in (({}, "/cwd", None), ("native", [], None), ("native", "/cwd", {})):
            with self.subTest(values=values), self.assertRaises(ValueError):
                NativeMetadata(*values)
