"""Session behavior tests with caller-owned dependencies."""

import dataclasses
import os
import shlex
import stat
import time
from unittest import mock

from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.engine.types import Access, ConfigError, SessionInfo, SessionRequest

from session.tests.unit.adapters.codex.support import CHILD, NATIVE, Base


class CommonTest(Base):
    def test_the_env_attributes(self):
        self.assertEqual(self.adapter.session_env, "CODEX_THREAD_ID")
        self.assertEqual(
            self.adapter.identity_env,
            (
                "CODEX_THREAD_ID",
                "CODEX_SESSION_ID",
                "CODEX_VERSION",
                "CODEX_SANDBOX",
                "CODEX_SANDBOX_NETWORK_DISABLED",
                "CODEX_PERMISSION_PROFILE",
            ),
        )
        self.assertEqual(
            self.adapter.child_defaults,
            {
                "NO_COLOR": "1",
                "CODEX_CI": "1",
                "PAGER": "cat",
                "GIT_PAGER": "cat",
                "GH_PAGER": "cat",
            },
        )

    def test_durable_snapshot_and_first_binding(self):
        with mock.patch("os.fsync", wraps=os.fsync) as sync:
            state = self.prepare()
        self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(state.snapshot["cwd"], str(self.cwd))
        self.assertIsNone(state.native_id)
        path = self.record()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        with mock.patch("os.fsync", wraps=os.fsync) as sync:
            bound = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(self.common.load(str(self.work), self.spec.sid), bound)
        self.assertEqual(self.common.bind(str(self.work), self.spec.sid, NATIVE), bound)
        with self.assertRaisesRegex(ConfigError, "bound|different"):
            self.common.bind(str(self.work), self.spec.sid, CHILD)
        with self.assertRaisesRegex(ConfigError, "already exists"):
            self.prepare()
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_bind_rejects_missing_or_invalid_native_id(self):
        self.prepare()
        for value in (None, "invalid", "11111111-2222-3333-4444-55555555555X"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "native ID"):
                self.common.bind(str(self.work), self.spec.sid, value)

    def test_resume_uses_saved_effective_settings(self):
        saved = self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        other = CodexAdapter(
            {
                "tiers": {"1": "changed"},
                "efforts": {"low": "high"},
                "env": {"CODEX_HOME": str(self.root / "elsewhere")},
            },
        )
        spec = dataclasses.replace(self.spec, access=Access(project=True), tier=None, effort=None)
        resumed = other.common.prepare(SessionRequest(spec, resumed=True))
        self.assertEqual(resumed.snapshot, saved.snapshot)
        launch = other.headless().launch("again", SessionRequest(spec, resumed=True))
        self.assertIn("gpt-test", launch.argv)
        self.assertIn("model_reasoning_effort=low", launch.argv)
        self.assertEqual(launch.env["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(launch.cwd, str(self.cwd))
        self.assertIn(NATIVE, launch.argv)

    def test_the_session_env_is_saved_and_wins_in_new_and_resumed_launches(self):
        env = {"PYTHONPATH": "/a b;c"}
        self.spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, env=env)
        )
        adapter = CodexAdapter({**self.adapter.config, "env": {"PYTHONPATH": "/config", "K": "v"}})
        launch = adapter.headless().launch("hi", SessionRequest(self.spec, resumed=False))
        self.assertEqual((launch.env["PYTHONPATH"], launch.env["K"]), ("/a b;c", "v"))
        self.assertEqual(
            self.common.load(str(self.work), self.spec.sid).snapshot["env"], {**env, "K": "v"}
        )
        state = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        spec = dataclasses.replace(self.spec, access=Access())
        resumed = adapter.headless().launch("again", SessionRequest(spec, resumed=True))
        self.assertEqual(resumed.env["PYTHONPATH"], "/a b;c")
        for command in (resumed.resume, self.common.resume_command(state)):
            q = shlex.quote
            self.assertTrue(
                command.startswith(
                    f"cd {q(str(self.cwd))} && CODEX_HOME={q(str(self.codex_home))} "
                    "PYTHONPATH='/a b;c' K=v codex resume "
                ),
                command,
            )
            self.assertEqual(shlex.split(command)[4], "PYTHONPATH=/a b;c")

    def test_both_modes_resume_with_saved_project_and_transcript(self):
        for saved in (False, True):
            with self.subTest(saved=saved):
                spec = dataclasses.replace(
                    self.spec,
                    sid=f"project-{saved}",
                    access=dataclasses.replace(self.spec.access, project=saved),
                )
                self.common.prepare(SessionRequest(spec, resumed=False))
                self.common.bind(str(self.work), spec.sid, NATIVE)
                path = self.rollout()
                requested = dataclasses.replace(
                    spec,
                    access=dataclasses.replace(spec.access, project=not saved),
                )
                for mode in ("headless", "tui"):
                    with (
                        self.subTest(mode=mode),
                        mock.patch("shutil.which", return_value="/fake/codex"),
                    ):
                        run = getattr(self.adapter, mode)()
                        launch = (
                            run.launch("again", SessionRequest(requested, resumed=True))
                            if mode == "headless"
                            else run.launch(
                                "again", SessionRequest(requested, resumed=True), ("/fake/hook",)
                            )
                        )
                    self.assertIs(launch.project, saved)
                    self.assertEqual(launch.transcript, str(path))
                    self.assertIn(NATIVE, launch.resume)

    def test_metadata_resolves_only_the_matching_trusted_binding(self):
        info = SessionInfo(
            self.spec.sid, NATIVE, str(self.cwd), project=False, transcript="", resume=""
        )
        self.assertEqual(self.adapter.session_metadata(info), info)
        self.prepare()
        self.assertEqual(self.adapter.session_metadata(info), info)
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        bound = self.adapter.session_metadata(info)
        self.assertEqual(bound.transcript, "")
        self.assertIn(NATIVE, bound.resume)
        path = self.rollout()
        resolved = self.adapter.session_metadata(bound)
        self.assertEqual(resolved.transcript, str(path))
        for unknown in (
            dataclasses.replace(resolved, native_id=CHILD),
            dataclasses.replace(resolved, sid="unknown"),
        ):
            self.assertEqual(self.adapter.session_metadata(unknown), unknown)
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            self.assertEqual(self.adapter.session_metadata(resolved), resolved)
        with mock.patch("os.stat", side_effect=PermissionError("denied")):
            self.assertEqual(self.adapter.session_metadata(resolved), resolved)

    def test_metadata_resume_carries_the_session_env(self):
        access = dataclasses.replace(self.spec.access, env={"PYTHONPATH": "/a b;c"})
        self.spec = dataclasses.replace(self.spec, access=access)
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        info = SessionInfo(
            self.spec.sid, NATIVE, str(self.cwd), project=False, transcript="", resume=""
        )
        resume = self.adapter.session_metadata(info).resume
        self.assertEqual(shlex.split(resume)[4], "PYTHONPATH=/a b;c")

    def test_omitted_mapping_preserves_runtime_defaults(self):
        spec = dataclasses.replace(self.spec, tier=None, effort=None)
        launch = self.adapter.headless().launch("hi", SessionRequest(spec, resumed=False))
        self.assertNotIn("-m", launch.argv)
        self.assertFalse(any(x.startswith("model_reasoning_effort=") for x in launch.argv))
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).snapshot["model"])

    def test_missing_recovery_and_session_fail_before_launch(self):
        with self.assertRaisesRegex(ConfigError, "missing.*state"):
            self.common.prepare(SessionRequest(self.spec, resumed=True))
        self.prepare()
        with self.assertRaisesRegex(ConfigError, "missing.*ID"):
            self.common.prepare(SessionRequest(self.spec, resumed=True))
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        with self.assertRaisesRegex(ConfigError, "missing.*file"):
            self.common.prepare(SessionRequest(self.spec, resumed=True))

    def test_fifo_record_is_nonblocking(self):
        self.prepare()
        path = self.record()
        path.unlink()
        os.mkfifo(path, 0o600)
        before = time.monotonic()
        with self.assertRaisesRegex(ConfigError, "unsafe"):
            self.common.prepare(SessionRequest(self.spec, resumed=True))
        self.assertLess(time.monotonic() - before, 0.5)

    def test_record_type_mode_owner_size_and_json_guards(self):
        self.prepare()
        path = self.record()
        original = path.read_text()
        for content in ("not json", "[" * 2000, "x" * (64 * 1024 + 1)):
            with self.subTest(content=content[:10]):
                path.write_text(content)
                with self.assertRaisesRegex(ConfigError, "malformed"):
                    self.common.load(str(self.work), self.spec.sid)
        path.write_text(original)
        path.chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "mode|owner"):
            self.common.load(str(self.work), self.spec.sid)
        path.chmod(0o600)
        with (
            mock.patch("os.getuid", return_value=os.getuid() + 1),
            self.assertRaisesRegex(ConfigError, "owner"),
        ):
            self.common.load(str(self.work), self.spec.sid)
        path.unlink()
        path.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(ConfigError, "symlink"):
            self.common.load(str(self.work), self.spec.sid)

    def test_record_identity_snapshot_and_grants_are_revalidated(self):
        state = self.prepare()
        original = self.record().read_text()
        cases = (
            {"sid": "forged"},
            {"snapshot": {**state.snapshot, "project": "yes"}},
            {"snapshot": {**state.snapshot, "cwd": str(self.work / ".." / "work")}},
            *(
                {"snapshot": {**state.snapshot, "env": env}}
                for env in (
                    {"X;Y": "1"},
                    {"A=B": "1"},
                    {"1A": "1"},
                    {"": "1"},
                    {"A": 1},
                    {"A": "b\0"},
                    [],
                )
            ),
            {"snapshot": {**state.snapshot, "other": 1}},
            {"writable_roots": [str(self.extra)]},
            {"writable_roots": [str(self.home)]},
            {"native_id": "not-a-uuid"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                self.record().write_text(original)
                self.edit_record(**fields)
                with self.assertRaises(ConfigError):
                    self.common.load(str(self.work), self.spec.sid)

    def test_store_outside_explicit_and_implicit_writable_roots(self):
        for grant in (self.home, self.home / ".agent-pm", self.root):
            with self.subTest(grant=grant), self.assertRaisesRegex(ConfigError, "writable"):
                self.common.prepare(
                    SessionRequest(
                        dataclasses.replace(self.spec, access=Access(dirs=(str(grant),))),
                        resumed=False,
                    )
                )
        with (
            mock.patch.dict(os.environ, {"HOME": "/tmp/agent-pm-untrusted-home"}),
            self.assertRaisesRegex(ConfigError, "writable"),
        ):
            self.common.load(str(self.work), self.spec.sid)

    def test_configured_tmpdir_cannot_grant_store_on_fresh_prepare(self):
        adapter = CodexAdapter(self.adapter.config | {"env": {"TMPDIR": str(self.home)}})
        with self.assertRaisesRegex(ConfigError, "writable"):
            adapter.common.prepare(SessionRequest(self.spec, resumed=False))
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_configured_tmpdir_cannot_grant_store_on_resumed_prepare(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        saved = self.record().read_bytes()
        adapter = CodexAdapter(self.adapter.config | {"env": {"TMPDIR": str(self.home)}})
        self.assertEqual(
            adapter.common.prepare(SessionRequest(self.spec, resumed=True)).native_id, NATIVE
        )
        self.assertEqual(self.record().read_bytes(), saved)

    def test_store_parent_permissions_and_symlink_guard(self):
        parent = self.home / ".agent-pm"
        parent.mkdir(mode=0o755)
        self.prepare()
        parent.chmod(0o775)
        with self.assertRaisesRegex(ConfigError, "mode|owner"):
            self.common.load(str(self.work), self.spec.sid)

    def test_store_parent_symlink_fails_before_creation(self):
        target = self.root / "store-target"
        target.mkdir()
        (self.home / ".agent-pm").symlink_to(target)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            self.prepare()
        self.assertEqual(list(target.iterdir()), [])

    def test_untrusted_project_configuration_and_cwd(self):
        settings = self.cwd / ".codex"
        settings.mkdir()
        with self.assertRaisesRegex(ConfigError, "project.*settings"):
            self.prepare()
        settings.rmdir()
        with self.assertRaisesRegex(ConfigError, "outside"):
            self.common.prepare(
                SessionRequest(
                    dataclasses.replace(self.spec, access=Access(cwd=str(self.extra))),
                    resumed=False,
                )
            )

    def test_mapping_keys_and_values_name_the_table(self):
        cases = (
            ("tiers", {"0": "SECRET"}),
            ("tiers", {"5": "SECRET"}),
            ("tiers", {"01": "SECRET"}),
            ("tiers", {1: "SECRET"}),
            ("tiers", {None: "SECRET"}),
            ("tiers", {"1": ""}),
            ("tiers", {"1": "  "}),
            ("tiers", {"1": "line\nfeed"}),
            ("tiers", {"1": 1}),
            ("efforts", {"turbo": "SECRET"}),
            ("efforts", {1: "SECRET"}),
            ("efforts", {None: "SECRET"}),
            ("efforts", {"low": ""}),
            ("efforts", {"low": "\0"}),
            ("efforts", {"low": None}),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError) as caught:
                    CodexAdapter(self.adapter.config | {key: value})
                self.assertIn(f"[adapters.codex].{key}", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))

    def test_valid_partial_mapping_keys_preserve_values(self):
        for key, values in (
            ("tiers", {"1": "one", "2": "two", "3": "three", "4": "four"}),
            ("efforts", {"low": "lo", "medium": "med", "high": "hi", "xhigh": "xh", "max": "mx"}),
        ):
            for source, target in values.items():
                with self.subTest(key=key, source=source):
                    adapter = CodexAdapter(self.adapter.config | {key: {source: target}})
                    self.assertEqual(adapter.common._mapped(key, source), target)
        CodexAdapter(self.adapter.config | {"tiers": {}, "efforts": {}})

    def test_invalid_environment_names_and_values_fail_before_launch(self):
        for key, value in (
            ("", "SECRET"),
            ("A=B", "SECRET"),
            ("A\0B", "SECRET"),
            (1, "SECRET"),
            ("A", "SECRET\0"),
            ("A", 1),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError) as caught:
                    CodexAdapter(self.adapter.config | {"env": {key: value}})
                self.assertIn("[adapters.codex].env", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))

    def test_config_validation_and_environment_isolation(self):
        for cfg in (
            {"shell_network": "yes"},
            {"tiers": {"1": "\n"}},
            {"efforts": []},
            {"env": {"A": 1}},
        ):
            with self.subTest(cfg=cfg), self.assertRaises(ConfigError):
                CodexAdapter({"tiers": {}, "efforts": {}, **cfg})
        adapter = CodexAdapter({"tiers": {}, "efforts": {}, "env": {"PROBE": "1"}})
        launch = adapter.headless().launch(
            "hi",
            SessionRequest(dataclasses.replace(self.spec, tier=None, effort=None), resumed=False),
        )
        self.assertEqual(launch.env["PROBE"], "1")
        self.assertEqual(launch.env["NO_COLOR"], "1")
        self.assertIn("CODEX_THREAD_ID", launch.unset)
        self.assertIn("CODEX_PERMISSION_PROFILE", launch.unset)
        self.assertNotIn("sandbox_workspace_write.writable_roots=[]", launch.argv)
        self.assertIsNotNone(adapter.tui())

    def test_missing_mapping_is_configuration_error(self):
        for key, spec in (
            ("tiers", dataclasses.replace(self.spec, effort=None)),
            ("efforts", dataclasses.replace(self.spec, tier=None)),
        ):
            with self.subTest(key=key), self.assertRaises(ConfigError) as caught:
                CodexAdapter({}).headless().launch("hi", SessionRequest(spec, resumed=False))
            self.assertIn(f"[adapters.codex].{key}", str(caught.exception))


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


class SavedFlags(Base):
    def test_invalid_saved_flags_fail_closed(self):
        state = self.prepare()
        for value in (None, True, 0, {}, "-s", [1], ["bad\0"]):
            self.edit_record(snapshot=state.snapshot | {"flags": value})
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "snapshot flags"):
                self.common.load(str(self.work), self.spec.sid)
