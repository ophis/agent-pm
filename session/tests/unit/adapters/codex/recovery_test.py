"""recovery test behavior."""

import dataclasses
import os
import shlex
import time
from unittest import mock

from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.engine.types import Access, ConfigError, SessionRequest

from session.tests.unit.adapters.codex.support import CHILD, NATIVE, Base


class WritableDirsTest(Base):
    def new_launch(self, adapter, home):
        roots = [str(path) for path in (self.cwd, home, self.extra)]
        dirs = adapter.writable_dirs(SessionRequest(self.spec, resumed=False))
        self.assertFalse((self.home / ".agent-pm").exists())
        self.assertEqual(list(dirs[:3]), roots)
        self.assertEqual(len(set(dirs)), len(dirs))
        launch = adapter.headless().launch("hi", SessionRequest(self.spec, resumed=False))
        self.assertEqual(launch.env["CODEX_HOME"], str(home))
        self.assertEqual(adapter.common.load(str(self.work), self.spec.sid).writable_roots, roots)

    def test_config_codex_home(self):
        home = self.root / "config-codex"
        self.new_launch(
            CodexAdapter(self.adapter.config | {"env": {"CODEX_HOME": str(home)}}), home
        )

    def test_environment_codex_home_real_path(self):
        home = self.root / "user-codex"
        home.mkdir()
        (self.root / "codex-link").symlink_to(home)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-link")}):
            self.new_launch(self.adapter, home)

    def test_tilde_codex_home_under_patched_home(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "~/user-codex"}):
            self.new_launch(self.adapter, self.home / "user-codex")

    def test_a_resume_reads_the_saved_roots(self):
        saved = self.prepare().writable_roots
        record = self.record().read_bytes()
        elsewhere = str(self.root / "elsewhere")
        other = CodexAdapter(self.adapter.config | {"env": {"CODEX_HOME": elsewhere}})
        spec = dataclasses.replace(self.spec, access=Access(project=True))
        self.assertIn(elsewhere, other.writable_dirs(SessionRequest(spec, resumed=False)))
        dirs = other.writable_dirs(SessionRequest(spec, resumed=True))
        self.assertEqual(list(dirs[: len(saved)]), saved)
        self.assertNotIn(elsewhere, dirs)
        self.assertEqual(self.record().read_bytes(), record)

    def test_read_dirs_are_never_added_saved_or_writable(self):
        read = self.root / "read"
        read.mkdir()
        adapter = CodexAdapter(self.adapter.config)
        access = dataclasses.replace(self.spec.access, reads=(str(read),))
        spec = dataclasses.replace(self.spec, access=access)
        argv = adapter.headless().launch("hi", SessionRequest(spec, resumed=False)).argv
        self.assertEqual(
            [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"], [str(self.extra)]
        )
        self.assertEqual(
            adapter.common.load(str(self.work), spec.sid).snapshot["dirs"], [str(self.extra)]
        )
        for resume in (False, True):
            with self.subTest(resume=resume):
                dirs = adapter.writable_dirs(SessionRequest(spec, resumed=resume))
                self.assertIn(str(self.extra), dirs)
                self.assertNotIn(str(read), dirs)

    def test_access_grants_are_passed_without_interpreting_policy(self):
        access = dataclasses.replace(self.spec.access, reads=(str(self.root),))
        spec = dataclasses.replace(self.spec, access=access)
        self.assertIn(
            "--add-dir",
            self.adapter.headless().launch("hi", SessionRequest(spec, resumed=False)).argv,
        )

    def test_the_session_env_adds_no_writable_dir(self):
        env = {"PYTHONPATH": str(self.root / "env-dir")}
        spec = dataclasses.replace(self.spec, access=dataclasses.replace(self.spec.access, env=env))
        fresh = self.adapter.writable_dirs(SessionRequest(self.spec, resumed=False))
        self.assertEqual(self.adapter.writable_dirs(SessionRequest(spec, resumed=False)), fresh)
        self.common.prepare(SessionRequest(spec, resumed=False))
        self.assertEqual(self.adapter.writable_dirs(SessionRequest(spec, resumed=True)), fresh)

    def test_a_resume_without_saved_state_is_refused(self):
        with self.assertRaisesRegex(
            ConfigError, "missing Codex recovery state for run ID 'caller-id'"
        ):
            self.adapter.writable_dirs(SessionRequest(self.spec, resumed=True))
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_a_configured_tmpdir_new_and_resumed(self):
        target = self.root / "config-tmp"
        target.mkdir()
        (self.root / "tmp-link").symlink_to(target)
        adapter = CodexAdapter(
            self.adapter.config | {"env": {"TMPDIR": str(self.root / "tmp-link")}}
        )
        self.assertNotIn(
            str(target), self.adapter.writable_dirs(SessionRequest(self.spec, resumed=False))
        )
        self.assertIn(str(target), adapter.writable_dirs(SessionRequest(self.spec, resumed=False)))
        adapter.common.prepare(SessionRequest(self.spec, resumed=False))
        self.assertIn(str(target), adapter.writable_dirs(SessionRequest(self.spec, resumed=True)))

    def test_fresh_query_translates_path_errors_without_persistence(self):
        spec = dataclasses.replace(self.spec, access=Access(dirs=("relative",)))
        with self.assertRaisesRegex(ConfigError, "absolute path"):
            self.adapter.writable_dirs(SessionRequest(spec, resumed=False))
        self.assertFalse((self.home / ".agent-pm").exists())


class SessionStatusTest(Base):
    def bound(self):
        state = self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        return state

    def test_inspection_queries_saved_cwd_in_one_store(self):
        self.bound()
        path = self.rollout()
        info = self.common.session_status(self.spec.sid, str(self.cwd))
        self.assertEqual((info.resumable, info.last_active), ("yes", 100))
        command = shlex.split(info.resume_command)
        self.assertIn(NATIVE, command)
        self.assertIn(str(self.cwd), command)
        self.assertEqual(self.common.session_status(self.spec.sid, str(self.work)).resumable, "no")
        path.unlink()
        self.assertEqual(self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "no")

    def test_inspection_missing_malformed_unsafe_and_ambiguous(self):
        self.assertEqual(self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "no")
        self.bound()
        self.rollout()
        path = self.record()
        path.write_text("not json")
        self.assertEqual(
            self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "unknown"
        )

    def test_unreadable_store_and_session_are_unknown(self):
        self.bound()
        self.rollout()
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            self.assertEqual(
                self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "unknown"
            )
        path = self.rollout()
        original = os.open

        def opening(location, *args, **kwargs):
            if str(location) == str(path):
                raise PermissionError("denied")
            return original(location, *args, **kwargs)

        with mock.patch("os.open", side_effect=opening):
            self.assertEqual(
                self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "unknown"
            )

    def test_transcript_fifo_mismatch_duplicate_and_symlink(self):
        self.bound()
        path = self.rollout()
        path.unlink()
        os.mkfifo(path)
        before = time.monotonic()
        with self.assertRaisesRegex(ConfigError, "unsafe"):
            self.common.prepare(SessionRequest(self.spec, resumed=True))
        self.assertLess(time.monotonic() - before, 0.5)
        path.unlink()
        path = self.rollout(cwd=str(self.extra))
        with self.assertRaisesRegex(ConfigError, "mismatched"):
            self.common.prepare(SessionRequest(self.spec, resumed=True))
        path = self.rollout()
        duplicate = path.with_name("rollout-other-" + NATIVE + ".jsonl")
        duplicate.write_text(path.read_text())
        with self.assertRaisesRegex(ConfigError, "duplicate"):
            self.common.prepare(SessionRequest(self.spec, resumed=True))
        duplicate.unlink()
        path.unlink()
        path.symlink_to(self.root / "absent")
        self.assertEqual(
            self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "unknown"
        )

    def test_last_activity_includes_validated_descendants(self):
        self.bound()
        self.rollout(stamp=100)
        self.rollout(CHILD, parent=NATIVE, stamp=200)
        self.rollout("bbbbbbbb-2222-3333-4444-555555555555", parent=CHILD, stamp=300)
        self.rollout("cccccccc-2222-3333-4444-555555555555", stamp=999)
        self.assertEqual(self.common.session_status(self.spec.sid, str(self.cwd)).last_active, 300)

    def test_duplicate_descendant_is_not_validated_activity(self):
        self.bound()
        self.rollout()
        child = self.rollout(CHILD, parent=NATIVE, stamp=200)
        duplicate = child.with_name("rollout-copy-" + CHILD + ".jsonl")
        duplicate.write_text(child.read_text())
        self.assertEqual(
            self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "unknown"
        )

    def test_ambiguous_sid_and_cwd_is_unknown(self):
        self.bound()
        self.rollout()
        other = self.root / "other"
        other.mkdir()
        self.common.prepare(
            SessionRequest(
                dataclasses.replace(
                    self.spec, workdir=str(other), access=Access(cwd=str(self.cwd), project=True)
                ),
                resumed=False,
            )
        )
        self.assertEqual(
            self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "unknown"
        )


class GrantTest(Base):
    def test_record_directory_is_only_writable_when_explicitly_granted(self):
        record = self.root / "worker-record"
        record.mkdir()
        for granted in (False, True):
            spec = dataclasses.replace(
                self.spec,
                workdir=str(record),
                sid=f"record-{granted}",
                access=Access(
                    cwd=str(self.cwd), project=True, dirs=(str(record),) if granted else ()
                ),
            )
            state = self.common.prepare(SessionRequest(spec, resumed=False))
            with self.subTest(granted=granted):
                self.assertEqual(str(record) in state.writable_roots, granted)
                self.assertEqual(
                    str(record) in self.adapter.writable_dirs(SessionRequest(spec, resumed=False)),
                    granted,
                )
                self.common.bind(str(record), spec.sid, NATIVE)
                self.rollout()
                resumed = self.common.prepare(SessionRequest(spec, resumed=True))
                self.assertEqual(str(record) in resumed.writable_roots, granted)
                self.assertEqual(
                    str(record) in self.adapter.writable_dirs(SessionRequest(spec, resumed=True)),
                    granted,
                )

    def test_access_and_flag_roots_are_combined_and_resume_ignores_new_flags(self):
        added = self.root / "added"
        spec = dataclasses.replace(self.spec, flags=("--add-dir", str(added)))
        state = self.common.prepare(SessionRequest(spec, resumed=False))
        expected = [
            str(self.cwd),
            str(self.codex_home),
            str(self.extra),
            str(added),
        ]
        self.assertEqual(state.writable_roots, expected)
        self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        changed = dataclasses.replace(spec, flags=("--add-dir", "/changed"))
        self.assertEqual(
            self.common.prepare(SessionRequest(changed, resumed=True)).writable_roots, expected
        )
        self.assertNotIn(
            "/changed", self.adapter.writable_dirs(SessionRequest(changed, resumed=True))
        )

    def test_recovery_needs_roots_covering_cwd_and_home(self):
        self.prepare()
        self.edit_record(writable_roots=[str(self.codex_home)])
        with self.assertRaisesRegex(ConfigError, "no recorded root covers"):
            self.common.load(str(self.work), self.spec.sid)

    def test_project_settings_guard_applies_to_every_caller(self):
        (self.cwd / ".codex").mkdir()
        with self.assertRaisesRegex(ConfigError, "project.*settings"):
            self.prepare()
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, project=True)
        )
        self.common.prepare(SessionRequest(spec, resumed=False))
