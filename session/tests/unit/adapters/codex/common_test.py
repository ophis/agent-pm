"""Session behavior tests with caller-owned dependencies."""

import contextlib
import dataclasses
import io
import json
import os
import re
import shlex
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.adapters.codex.common import (
    NativeMetadata,
    RecoveryError,
    SessionStore,
    read_metadata,
    snapshot_data,
    validate_snapshot,
)
from agent_pm.session.engine.types import Access, ConfigError, SessionInfo, SessionStatus

from session.tests.unit.adapters.codex.support import CHILD, NATIVE, Base, committed_headless


class LaunchLifecycleTest(Base):
    def test_preparation_and_commands_are_pure_until_commit_in_both_modes(self):
        for mode in ("headless", "tui"):
            spec = replace(self.spec, sid=mode, prompt="hello")
            run = self.adapter.headless() if mode == "headless" else self.adapter.tui()
            with mock.patch("shutil.which", return_value="/protected/codex"):
                if mode == "headless":
                    run.launch(spec)
                else:
                    run.launch(spec, (sys.executable, "-I", "/protected/hook.py"))
            self.assertIsNone(self.common.load(spec.workdir, spec.sid))
            run.commit()
            self.assertIsNotNone(self.common.load(spec.workdir, spec.sid))

    def test_invalid_launches_write_no_recovery_or_run_records(self):
        for mode in ("headless", "tui"):
            spec = replace(self.spec, sid=mode, prompt="bad\0prompt")
            run = self.adapter.headless() if mode == "headless" else self.adapter.tui()
            with (
                self.subTest(mode=mode),
                mock.patch("shutil.which", return_value="/protected/codex"),
                self.assertRaises(ConfigError),
            ):
                if mode == "headless":
                    run.launch(spec)
                else:
                    run.launch(spec, (sys.executable, "-I", "/protected/hook.py"))
            self.assertFalse((self.home / ".agent-pm").exists())


class CommonTest(Base):
    def test_prepare_resolves_each_access_dir_once(self):
        linked = self.root / "extra-link"
        linked.symlink_to(self.extra, target_is_directory=True)
        spec = replace(self.spec, access=replace(self.spec.access, dirs=(str(linked),)))
        with mock.patch("os.path.realpath", wraps=os.path.realpath) as realpath:
            state = self.common.prepare(spec)
        self.assertEqual(state.snapshot.dirs, (str(self.extra),))
        self.assertIn(str(self.extra), state.writable_roots)
        self.assertEqual(realpath.call_args_list.count(mock.call(str(linked))), 1)
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_prepare_still_validates_project_and_commands_before_saving(self):
        for fields, message in (
            ({"project": 1}, "snapshot project"),
            ({"commands": ("",)}, "snapshot commands"),
            ({"commands": ("git\nstatus",)}, "snapshot commands"),
        ):
            spec = replace(self.spec, access=replace(self.spec.access, **fields))
            with self.subTest(fields=fields), self.assertRaisesRegex(ConfigError, message):
                self.common.prepare(spec)
            self.assertFalse((self.home / ".agent-pm").exists())

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
        self.assertEqual(state.snapshot.cwd, str(self.cwd))
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

    def test_an_unmapped_tier_or_effort_is_named_by_its_plain_value(self):
        for changed, message in (
            ({"effort": "high"}, "no mapping for 'high' in [adapters.codex].efforts"),
            ({"tier": 2}, "no mapping for 2 in [adapters.codex].tiers"),
        ):
            spec = dataclasses.replace(self.spec, **changed)
            with self.subTest(changed=changed), self.assertRaises(ConfigError) as caught:
                self.common.save(self.common.prepare(spec))
            self.assertEqual(str(caught.exception), message)

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
        resumed = other.common.prepare(dataclasses.replace(spec, resume=True))
        self.assertEqual(resumed.snapshot, saved.snapshot)
        launch = committed_headless(other, dataclasses.replace(spec, prompt="again", resume=True))
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
        launch = committed_headless(adapter, dataclasses.replace(self.spec, prompt="hi"))
        self.assertEqual((launch.env["PYTHONPATH"], launch.env["K"]), ("/a b;c", "v"))
        self.assertEqual(
            self.common.load(str(self.work), self.spec.sid).snapshot.env, {**env, "K": "v"}
        )
        state = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        spec = dataclasses.replace(self.spec, access=Access())
        resumed = committed_headless(
            adapter, dataclasses.replace(spec, prompt="again", resume=True)
        )
        self.assertEqual(resumed.env["PYTHONPATH"], "/a b;c")
        self.assertEqual(resumed.resume, self.common.resume_command(state))
        env = self.common.resume_launch(state).env
        self.assertEqual(
            (env["PYTHONPATH"], env["K"], env["CODEX_HOME"]), ("/a b;c", "v", str(self.codex_home))
        )

    def test_both_modes_resume_with_saved_project_and_transcript(self):
        for saved in (False, True):
            with self.subTest(saved=saved):
                spec = dataclasses.replace(
                    self.spec,
                    sid=f"project-{saved}",
                    access=dataclasses.replace(self.spec.access, project=saved),
                )
                self.common.save(self.common.prepare(spec))
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
                            run.launch(dataclasses.replace(requested, prompt="again", resume=True))
                            if mode == "headless"
                            else run.launch(
                                dataclasses.replace(requested, prompt="again", resume=True),
                                ("/fake/hook",),
                            )
                        )
                    self.assertIs(launch.project, saved)
                    self.assertEqual(launch.transcript, str(path))
                    self.assertIn(NATIVE, launch.resume)

    def test_metadata_resolves_the_sids_binding_from_the_store(self):
        info = SessionInfo(self.spec.sid, str(self.cwd), project=False, transcript="", resume="")
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
            dataclasses.replace(resolved, cwd=str(self.extra)),
            dataclasses.replace(resolved, sid="unknown"),
        ):
            self.assertEqual(self.adapter.session_metadata(unknown), unknown)
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            self.assertEqual(self.adapter.session_metadata(resolved), resolved)
        with mock.patch("os.stat", side_effect=PermissionError("denied")):
            self.assertEqual(self.adapter.session_metadata(resolved), resolved)

    def test_metadata_resume_replays_the_session_env(self):
        access = dataclasses.replace(self.spec.access, env={"PYTHONPATH": "/a b;c"})
        self.spec = dataclasses.replace(self.spec, access=access)
        self.prepare()
        state = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        info = SessionInfo(self.spec.sid, str(self.cwd), project=False, transcript="", resume="")
        resume = self.adapter.session_metadata(info).resume
        self.assertEqual(resume, self.common.resume_command(state))
        self.assertNotIn("/a b;c", resume)
        self.assertEqual(self.common.resume_launch(state).env["PYTHONPATH"], "/a b;c")

    def test_omitted_mapping_preserves_client_defaults(self):
        spec = dataclasses.replace(self.spec, tier=None, effort=None)
        launch = committed_headless(self.adapter, dataclasses.replace(spec, prompt="hi"))
        self.assertNotIn("-m", launch.argv)
        self.assertFalse(any(x.startswith("model_reasoning_effort=") for x in launch.argv))
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).snapshot.model)

    def test_missing_recovery_and_session_fail_before_launch(self):
        with self.assertRaisesRegex(ConfigError, "missing.*state"):
            self.common.prepare(dataclasses.replace(self.spec, resume=True))
        self.prepare()
        with self.assertRaisesRegex(ConfigError, "missing.*ID"):
            self.common.prepare(dataclasses.replace(self.spec, resume=True))
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        with self.assertRaisesRegex(ConfigError, "missing.*file"):
            self.common.prepare(dataclasses.replace(self.spec, resume=True))

    def test_fifo_record_is_nonblocking(self):
        self.prepare()
        path = self.record()
        path.unlink()
        os.mkfifo(path, 0o600)
        before = time.monotonic()
        with self.assertRaisesRegex(ConfigError, "not a regular"):
            self.common.prepare(dataclasses.replace(self.spec, resume=True))
        self.assertLess(time.monotonic() - before, 0.5)

    def test_record_size_and_json_guards(self):
        self.prepare()
        path = self.record()
        original = path.read_text()
        for content in ("not json", "[" * 2000, "x" * (64 * 1024 + 1)):
            with self.subTest(content=content[:10]):
                path.write_text(content)
                with self.assertRaisesRegex(ConfigError, "malformed"):
                    self.common.load(str(self.work), self.spec.sid)
        path.write_text(original)
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).sid, self.spec.sid)

    def test_record_identity_snapshot_and_grants_are_revalidated(self):
        state = self.prepare()
        original = self.record().read_text()
        cases = (
            {"sid": "forged"},
            {"snapshot": {**snapshot_data(state.snapshot), "project": "yes"}},
            {"snapshot": {**snapshot_data(state.snapshot), "cwd": str(self.work / ".." / "work")}},
            *(
                {"snapshot": {**snapshot_data(state.snapshot), "env": env}}
                for env in (
                    {"X=Y": "1"},
                    {"A=B": "1"},
                    {"A\0": "1"},
                    {"": "1"},
                    {"A": 1},
                    {"A": "b\0"},
                    [],
                )
            ),
            {"snapshot": {**snapshot_data(state.snapshot), "other": 1}},
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
                with self.assertRaises(ConfigError), contextlib.redirect_stderr(io.StringIO()):
                    self.common.lookup(self.spec.sid, str(self.cwd))

    def test_store_outside_explicit_and_implicit_writable_roots(self):
        for grant in (self.home, self.home / ".agent-pm", self.root):
            with self.subTest(grant=grant), self.assertRaisesRegex(ConfigError, "writable"):
                self.common.save(
                    self.common.prepare(
                        dataclasses.replace(self.spec, access=Access(dirs=(str(grant),)))
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
            adapter.common.save(adapter.common.prepare(self.spec))
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_configured_tmpdir_cannot_grant_store_on_resumed_prepare(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        saved = self.record().read_bytes()
        adapter = CodexAdapter(self.adapter.config | {"env": {"TMPDIR": str(self.home)}})
        self.assertEqual(
            adapter.common.prepare(dataclasses.replace(self.spec, resume=True)).native_id, NATIVE
        )
        self.assertEqual(self.record().read_bytes(), saved)

    def test_untrusted_project_configuration_and_cwd(self):
        settings = self.cwd / ".codex"
        settings.mkdir()
        with self.assertRaisesRegex(ConfigError, "project.*settings"):
            self.prepare()
        settings.rmdir()
        with self.assertRaisesRegex(ConfigError, "outside"):
            self.common.save(
                self.common.prepare(
                    dataclasses.replace(self.spec, access=Access(cwd=str(self.extra)))
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
        launch = committed_headless(
            adapter, dataclasses.replace(self.spec, tier=None, effort=None, prompt="hi")
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
                committed_headless(CodexAdapter({}), dataclasses.replace(spec, prompt="hi"))
            self.assertIn(f"[adapters.codex].{key}", str(caught.exception))


class EnvironmentTest(Base):
    def test_explicit_source_precedence_matches_snapshot_launch_and_inspection(self):
        ambient_tmp, config_tmp, access_tmp, spec_tmp = (
            str(self.root / name)
            for name in ("ambient-tmp", "config-tmp", "access-tmp", "spec-tmp")
        )
        configured = {
            "SOURCE": "config",
            "CONFIG_ONLY": "config",
            "CODEX_HOME": str(self.root / "config-home"),
            "TMPDIR": config_tmp,
            "PAGER": "configured",
            "CODEX_SESSION_ID": "explicit-marker",
        }
        access = {
            "SOURCE": "access",
            "ACCESS_ONLY": "access",
            "CODEX_HOME": str(self.root / "access-home"),
            "TMPDIR": access_tmp,
            "NO_COLOR": "access",
        }
        supplied = {
            "SOURCE": "spec",
            "SPEC_ONLY": "spec",
            "CODEX_HOME": str(self.codex_home),
            "TMPDIR": spec_tmp,
            "CODEX_CI": "explicit",
        }
        adapter = CodexAdapter(self.adapter.config | {"env": configured})
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, env=access), env=supplied
        )
        with mock.patch.dict(
            os.environ,
            {
                "SOURCE": "ambient",
                "AMBIENT_ONLY": "ambient",
                "CODEX_HOME": str(self.root / "ambient-home"),
                "TMPDIR": ambient_tmp,
                "PAGER": "ambient",
                "CODEX_THREAD_ID": NATIVE,
                "CODEX_SESSION_ID": "ambient-marker",
            },
        ):
            roots = adapter.writable_dirs(spec)
            launch = committed_headless(adapter, dataclasses.replace(spec, prompt="hi"))
            state = adapter.common.load(spec.workdir, spec.sid)
            self.assertEqual(state.snapshot.env, {**configured, **access, **supplied})
            self.assertEqual(state.snapshot.codex_home, str(self.codex_home))
            self.assertEqual(launch.env["SOURCE"], "spec")
            self.assertEqual(launch.env["PAGER"], "configured")
            self.assertEqual(launch.env["NO_COLOR"], "access")
            self.assertEqual(launch.env["CODEX_CI"], "explicit")
            self.assertEqual(launch.env["GIT_PAGER"], "cat")
            self.assertNotIn("AMBIENT_ONLY", launch.env)
            self.assertIn(spec_tmp, roots)
            for ignored in (ambient_tmp, config_tmp, access_tmp):
                self.assertNotIn(ignored, roots)
            effective = adapter.common.environment(state.snapshot, ambient=True)
            self.assertEqual(effective["AMBIENT_ONLY"], "ambient")
            self.assertEqual(effective["TMPDIR"], spec_tmp)
            self.assertNotIn("CODEX_THREAD_ID", effective)
            self.assertEqual(effective["CODEX_SESSION_ID"], "explicit-marker")
            self.assertEqual(adapter.writable_dirs(dataclasses.replace(spec, resume=True)), roots)

    def test_resume_replays_explicit_inputs_and_refreshes_ambient_values(self):
        first_tmp, second_tmp, ignored_tmp = (
            str(self.root / name) for name in ("first-tmp", "second-tmp", "ignored-tmp")
        )
        with mock.patch.dict(os.environ, {"TMPDIR": first_tmp, "PAGER": "first-pager"}):
            inspected = self.adapter.writable_dirs(self.spec)
            saved = self.prepare()
            self.assertEqual(saved.snapshot.env, {})
            self.assertIn(first_tmp, inspected)
            self.assertEqual(self.common.environment(saved.snapshot)["PAGER"], "cat")
            self.assertNotIn("PAGER", self.common.environment(saved.snapshot, interactive=True))
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        changed = CodexAdapter(self.adapter.config | {"env": {"TMPDIR": ignored_tmp}})
        spec = dataclasses.replace(self.spec, env={"TMPDIR": ignored_tmp, "PAGER": "ignored"})
        with mock.patch.dict(
            os.environ,
            {
                "TMPDIR": second_tmp,
                "PAGER": "second-pager",
                "CODEX_HOME": str(self.root / "changed-home"),
                "CODEX_THREAD_ID": NATIVE,
            },
        ):
            resumed = changed.common.prepare(dataclasses.replace(spec, resume=True))
            self.assertEqual(resumed.snapshot, saved.snapshot)
            roots = changed.writable_dirs(dataclasses.replace(spec, resume=True))
            self.assertIn(second_tmp, roots)
            self.assertNotIn(first_tmp, roots)
            self.assertNotIn(ignored_tmp, roots)
            launch = committed_headless(
                changed, dataclasses.replace(spec, prompt="again", resume=True)
            )
            self.assertEqual(launch.env["CODEX_HOME"], str(self.codex_home))
            self.assertEqual(launch.env["PAGER"], "cat")
            self.assertNotIn("TMPDIR", launch.env)
            effective = changed.common.environment(resumed.snapshot, ambient=True)
            interactive = changed.common.environment(
                resumed.snapshot, ambient=True, interactive=True
            )
            self.assertEqual(effective["TMPDIR"], second_tmp)
            self.assertEqual(effective["PAGER"], "cat")
            self.assertEqual(interactive["PAGER"], "second-pager")
            self.assertEqual(interactive["CODEX_HOME"], str(self.codex_home))
            self.assertNotIn("CODEX_THREAD_ID", interactive)

    def test_empty_explicit_codex_home_preserves_the_ambient_fallback(self):
        adapter = CodexAdapter(self.adapter.config | {"env": {"CODEX_HOME": ""}})
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}):
            inspected = adapter.writable_dirs(self.spec)
            state = adapter.common.prepare(self.spec)
            self.assertEqual(state.snapshot.env, {"CODEX_HOME": ""})
            self.assertEqual(state.snapshot.codex_home, str(self.codex_home))
            self.assertEqual(
                adapter.common.environment(state.snapshot)["CODEX_HOME"], str(self.codex_home)
            )
            self.assertEqual(inspected[1], str(self.codex_home))


class WritableDirsTest(Base):
    def new_launch(self, adapter, home):
        roots = [str(path) for path in (self.cwd, home, self.extra)]
        dirs = adapter.writable_dirs(self.spec)
        self.assertFalse((self.home / ".agent-pm").exists())
        self.assertEqual(list(dirs[:3]), roots)
        self.assertEqual(len(set(dirs)), len(dirs))
        launch = committed_headless(adapter, dataclasses.replace(self.spec, prompt="hi"))
        self.assertEqual(launch.env["CODEX_HOME"], str(home))
        self.assertEqual(
            adapter.common.load(str(self.work), self.spec.sid).writable_roots, tuple(roots)
        )

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
        self.assertIn(elsewhere, other.writable_dirs(spec))
        dirs = other.writable_dirs(dataclasses.replace(spec, resume=True))
        self.assertEqual(tuple(dirs[: len(saved)]), saved)
        self.assertNotIn(elsewhere, dirs)
        self.assertEqual(self.record().read_bytes(), record)

    def test_read_dirs_are_never_added_saved_or_writable(self):
        read = self.root / "read"
        read.mkdir()
        adapter = CodexAdapter(self.adapter.config)
        access = dataclasses.replace(self.spec.access, reads=(str(read),))
        spec = dataclasses.replace(self.spec, access=access)
        argv = committed_headless(adapter, dataclasses.replace(spec, prompt="hi")).argv
        self.assertEqual(
            [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"], [str(self.extra)]
        )
        self.assertEqual(
            adapter.common.load(str(self.work), spec.sid).snapshot.dirs, (str(self.extra),)
        )
        for resume in (False, True):
            with self.subTest(resume=resume):
                dirs = adapter.writable_dirs(dataclasses.replace(spec, resume=resume))
                self.assertIn(str(self.extra), dirs)
                self.assertNotIn(str(read), dirs)

    def test_access_grants_are_passed_without_interpreting_policy(self):
        access = dataclasses.replace(self.spec.access, reads=(str(self.root),))
        spec = dataclasses.replace(self.spec, access=access)
        self.assertIn(
            "--add-dir",
            committed_headless(self.adapter, dataclasses.replace(spec, prompt="hi")).argv,
        )

    def test_the_session_env_adds_no_writable_dir(self):
        env = {"PYTHONPATH": str(self.root / "env-dir")}
        spec = dataclasses.replace(self.spec, access=dataclasses.replace(self.spec.access, env=env))
        fresh = self.adapter.writable_dirs(self.spec)
        self.assertEqual(self.adapter.writable_dirs(spec), fresh)
        self.common.save(self.common.prepare(spec))
        self.assertEqual(self.adapter.writable_dirs(dataclasses.replace(spec, resume=True)), fresh)

    def test_a_resume_without_saved_state_is_refused(self):
        with self.assertRaisesRegex(
            ConfigError, "missing Codex recovery state for run ID 'caller-id'"
        ):
            self.adapter.writable_dirs(dataclasses.replace(self.spec, resume=True))
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_a_configured_tmpdir_new_and_resumed(self):
        target = self.root / "config-tmp"
        target.mkdir()
        (self.root / "tmp-link").symlink_to(target)
        adapter = CodexAdapter(
            self.adapter.config | {"env": {"TMPDIR": str(self.root / "tmp-link")}}
        )
        self.assertNotIn(str(target), self.adapter.writable_dirs(self.spec))
        self.assertIn(str(target), adapter.writable_dirs(self.spec))
        adapter.common.save(adapter.common.prepare(self.spec))
        self.assertIn(
            str(target), adapter.writable_dirs(dataclasses.replace(self.spec, resume=True))
        )

    def test_resume_query_translates_a_bad_recovery_file_without_writing_store(self):
        spec = dataclasses.replace(self.spec, resume=True)
        self.prepare()
        path = self.record()
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaisesRegex(ConfigError, "not a regular"):
            self.adapter.writable_dirs(spec)
        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertFalse((self.work / "run.jsonl").exists())


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
        with contextlib.redirect_stderr(io.StringIO()):
            status = self.common.session_status(self.spec.sid, str(self.cwd))
        self.assertEqual(status.resumable, "unknown")

    def test_a_bad_recovery_file_hides_no_other_run(self):
        self.bound()
        self.rollout()
        bad = self.record().with_name("0" * 64 + ".json")
        bad.write_text("not json")
        bad.chmod(0o600)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            found = self.common.session_status(self.spec.sid, str(self.cwd))
            unrecorded = self.common.session_status("unrecorded", str(self.cwd))
        self.assertEqual(found.resumable, "yes")
        self.assertEqual(unrecorded.resumable, "unknown")
        line = f"codex recovery: malformed recovery state: {bad}"
        self.assertEqual(err.getvalue().splitlines(), [line, line])

    def test_a_skipped_files_line_names_it(self):
        self.bound()
        bad = self.record().with_name("1" * 64 + ".json")
        bad.write_text("{}")
        bad.chmod(0o600)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(
                self.common.session_status(self.spec.sid, str(self.cwd)).resumable, "no"
            )
        (line,) = err.getvalue().splitlines()
        self.assertTrue(line.startswith(f"codex recovery: {bad}: invalid"), line)

    def test_a_missing_recorded_transcript_is_no_without_discovery_fallback(self):
        self.bound()
        resume = self.common.session_status(self.spec.sid, str(self.cwd)).resume_command
        path = self.rollout()
        path.rename(path.with_name("rollout-other-" + NATIVE + ".jsonl"))
        status = self.common.session_status(self.spec.sid, str(self.cwd), transcript=str(path))
        self.assertEqual(status, SessionStatus("no", None, resume))

    def test_an_unreadable_recorded_transcript_is_unknown(self):
        self.bound()
        path = self.rollout()
        resume = self.common.session_status(self.spec.sid, str(self.cwd)).resume_command
        original = os.open

        def opening(location, *args, **kwargs):
            if str(location) == str(path):
                raise PermissionError("denied")
            return original(location, *args, **kwargs)

        with mock.patch("os.open", side_effect=opening):
            status = self.common.session_status(self.spec.sid, str(self.cwd), transcript=str(path))
        self.assertEqual(status, SessionStatus("unknown", None, resume))

    def test_malformed_recorded_transcript_paths_are_unknown(self):
        self.bound()
        self.rollout()
        resume = self.common.session_status(self.spec.sid, str(self.cwd)).resume_command
        for path in ("relative.jsonl", "\0"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.common.session_status(self.spec.sid, str(self.cwd), transcript=path),
                    SessionStatus("unknown", None, resume),
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

    def test_transcript_fifo_mismatch_and_duplicate(self):
        self.bound()
        path = self.rollout()
        path.unlink()
        os.mkfifo(path)
        before = time.monotonic()
        with self.assertRaisesRegex(ConfigError, "not a regular"):
            self.common.prepare(dataclasses.replace(self.spec, resume=True))
        self.assertLess(time.monotonic() - before, 0.5)
        path.unlink()
        path = self.rollout(cwd=str(self.extra))
        with self.assertRaisesRegex(ConfigError, "mismatched"):
            self.common.prepare(dataclasses.replace(self.spec, resume=True))
        path = self.rollout()
        duplicate = path.with_name("rollout-other-" + NATIVE + ".jsonl")
        duplicate.write_text(path.read_text())
        with self.assertRaisesRegex(ConfigError, "duplicate"):
            self.common.prepare(dataclasses.replace(self.spec, resume=True))

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
        self.common.save(
            self.common.prepare(
                dataclasses.replace(
                    self.spec, workdir=str(other), access=Access(cwd=str(self.cwd), project=True)
                )
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
            state = self.common.save(self.common.prepare(spec))
            with self.subTest(granted=granted):
                self.assertEqual(str(record) in state.writable_roots, granted)
                self.assertEqual(
                    str(record) in self.adapter.writable_dirs(spec),
                    granted,
                )
                self.common.bind(str(record), spec.sid, NATIVE)
                self.rollout()
                resumed = self.common.prepare(dataclasses.replace(spec, resume=True))
                self.assertEqual(str(record) in resumed.writable_roots, granted)
                self.assertEqual(
                    str(record)
                    in self.adapter.writable_dirs(dataclasses.replace(spec, resume=True)),
                    granted,
                )

    def test_access_and_flag_roots_are_combined_and_resume_ignores_new_flags(self):
        added = self.root / "added"
        spec = dataclasses.replace(self.spec, flags=("--add-dir", str(added)))
        state = self.common.save(self.common.prepare(spec))
        expected = [
            str(self.cwd),
            str(self.codex_home),
            str(self.extra),
            str(added),
        ]
        self.assertEqual(state.writable_roots, tuple(expected))
        self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        changed = dataclasses.replace(spec, flags=("--add-dir", "/changed"))
        self.assertEqual(
            self.common.prepare(dataclasses.replace(changed, resume=True)).writable_roots,
            tuple(expected),
        )
        self.assertNotIn(
            "/changed", self.adapter.writable_dirs(dataclasses.replace(changed, resume=True))
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
        self.common.save(self.common.prepare(spec))


class SavedFlags(Base):
    def test_invalid_saved_flags_fail_closed(self):
        state = self.prepare()
        for value in (None, True, 0, {}, "-s", [1], ["bad\0"]):
            self.edit_record(snapshot=snapshot_data(state.snapshot) | {"flags": value})
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "snapshot flags"):
                self.common.load(str(self.work), self.spec.sid)


GOOD = (
    {},
    {
        "PYTHONPATH": "/a b;c",
        "_x1": "",
        "A-B": "v",
        "X;Y": "v",
        "1A": "v",
        "A B": "v",
        "A\n": "v",
        "Ä": "v",
    },
)
BAD = (
    {"A=B": "a"},
    {"A\0": "a"},
    {"": "a"},
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
            **snapshot_data(self.common.prepare(self.spec).snapshot),
            **fields,
        }

    def test_keeps_an_env_access_env_accepts(self):
        for env in GOOD:
            with self.subTest(env=env):
                self.assertEqual(Access(env=env).env, env)
                self.assertEqual(CodexAdapter({"env": env}).config["env"], env)
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
    def test_records_live_in_sessions_codex_under_the_state_directory(self):
        state = self.prepare()
        store = self.home / ".agent-pm" / "sessions" / "codex"
        self.assertEqual(SessionStore().root, str(store))
        self.assertEqual([path.parent for path in store.parent.parent.rglob("*.json")], [store])
        self.assertEqual(SessionStore().load(state.workdir, state.sid), state)

    def test_a_record_only_in_the_old_directory_is_neither_read_nor_moved(self):
        state = self.common.prepare(self.spec)
        old = self.home / ".agent-pm" / "core-new-sessions" / "codex"
        SessionStore(str(old)).save(state)
        before = {path: path.read_bytes() for path in old.iterdir()}
        store = SessionStore()
        self.assertIsNone(store.load(state.workdir, state.sid))
        self.assertIsNone(store.lookup(state.sid, state.snapshot.cwd))
        self.assertIsNone(self.common.load(state.workdir, state.sid))
        self.assertEqual({path: path.read_bytes() for path in old.iterdir()}, before)
        self.assertFalse((self.home / ".agent-pm" / "sessions").exists())

    def test_invalid_save_does_not_create_the_store(self):
        state = self.common.prepare(self.spec)
        unsafe = replace(state, writable_roots=(*state.writable_roots, str(self.home)))
        with self.assertRaisesRegex(RecoveryError, "writable"):
            SessionStore().save(unsafe)
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_each_operation_resolves_the_default_root_once(self):
        state = self.prepare()
        store = SessionStore()
        operations = (
            lambda: store.directory(create=False),
            lambda: store.create(state.workdir, "new", state.snapshot, state.writable_roots),
            lambda: store.save(replace(state, sid="saved")),
            lambda: store.load(state.workdir, state.sid),
            lambda: store.bind(state.workdir, state.sid, NATIVE),
            lambda: store.lookup(state.sid, state.snapshot.cwd),
        )
        for operation in operations:
            with (
                self.subTest(operation=operation),
                mock.patch("os.path.expanduser", wraps=os.path.expanduser) as expanduser,
            ):
                operation()
                expanduser.assert_called_once_with("~")

    def test_existing_store_resolves_and_validates_a_changed_home_per_operation(self):
        state = self.prepare()
        store = SessionStore()
        self.assertEqual(store.load(state.workdir, state.sid), state)
        other_home = self.root / "other-home"
        other_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(other_home)}):
            self.assertIsNone(store.load(state.workdir, state.sid))
            store.save(store.create(state.workdir, state.sid, state.snapshot, state.writable_roots))
            self.assertEqual(store.load(state.workdir, state.sid), state)


class CodecTest(Base):
    def test_malformed_snapshot_fields_are_rejected_when_loading_saved_json(self):
        state = self.prepare()
        original = snapshot_data(state.snapshot)
        for fields in (
            {"cwd": 1},
            {"codex_home": "relative"},
            {"dirs": "not-a-list"},
            {"dirs": ["relative"]},
            {"commands": "not-a-list"},
            {"commands": [1]},
            {"commands": [""]},
            {"commands": ["git\nstatus"]},
            {"model": float("nan")},
            {"model": ""},
            {"effort": []},
            {"effort": "bad\nvalue"},
        ):
            with self.subTest(fields=fields):
                self.edit_record(snapshot=original | fields)
                with self.assertRaises(ConfigError):
                    self.common.load(state.workdir, state.sid)

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


SECRETS = {"CFG_TOKEN": "cfg-secret-1", "ACCESS_TOKEN": "access-secret-2", "SPEC_TOKEN": "spec-3"}


class ResumeReferenceTest(Base):
    def setUp(self):
        super().setUp()
        self.adapter = CodexAdapter({**self.adapter.config, "env": {"CFG_TOKEN": "cfg-secret-1"}})
        self.common = self.adapter.common
        self.spec = dataclasses.replace(
            self.spec,
            flags=("-s", "danger-full-access"),
            env={"SPEC_TOKEN": "spec-3"},
            access=dataclasses.replace(self.spec.access, env={"ACCESS_TOKEN": "access-secret-2"}),
        )

    def bound(self):
        self.prepare()
        return self.common.bind(str(self.work), self.spec.sid, NATIVE)

    def assert_public(self, text):
        for value in SECRETS.values():
            self.assertNotIn(value, text)
        assigned = [w for w in shlex.split(text) if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", w)]
        self.assertEqual(assigned, [], text)
        self.assertNotIn("danger-full-access", text)

    def test_reference_names_store_locators_and_no_values(self):
        command = self.common.resume_command(self.bound())
        locators = [SessionStore().root, str(self.work), self.spec.sid, NATIVE]
        argv = ["agent-pm-session", "client-resume", "codex", *locators]
        self.assertEqual(command, f"cd {shlex.quote(str(self.cwd))} && {shlex.join(argv)}")
        self.assert_public(command)
        info = SessionInfo(self.spec.sid, str(self.cwd), project=False, transcript="", resume="")
        self.assertEqual(self.adapter.session_metadata(info).resume, command)
        status = self.common.session_status(self.spec.sid, str(self.cwd))
        self.assertEqual(status.resume_command, command)

    def test_resumed_launches_publish_the_reference(self):
        state = self.bound()
        self.rollout()
        resumed = dataclasses.replace(self.spec, prompt="again", resume=True)
        with mock.patch("shutil.which", return_value="/fake/codex"):
            launches = {
                "headless": self.adapter.headless().launch(resumed),
                "tui": self.adapter.tui().launch(resumed, ("/fake/hook",)),
            }
        for mode, launch in launches.items():
            with self.subTest(mode):
                self.assertEqual(launch.resume, self.common.resume_command(state))
                self.assert_public(launch.resume)


FLAGS = ("-s", "danger-full-access", "-c", 'approval_policy="on-request"')


class ResumeMainTest(Base):
    def setUp(self):
        super().setUp()
        self.adapter = CodexAdapter({**self.adapter.config, "env": {"CFG_TOKEN": "cfg-secret-1"}})
        self.common = self.adapter.common
        self.spec = dataclasses.replace(
            self.spec,
            flags=FLAGS,
            env={"SPEC_TOKEN": "spec-3", "GIT_DIR": "/spec/git"},
            access=dataclasses.replace(self.spec.access, env={"ACCESS_TOKEN": "access-secret-2"}),
        )
        self.store = SessionStore().root

    def bound(self, native=NATIVE):
        self.prepare()
        return self.common.bind(str(self.work), self.spec.sid, native)

    def run_main(self, native=NATIVE, *, store=None):
        stderr = io.StringIO()
        with (
            mock.patch("os.execvpe") as execvpe,
            mock.patch("os.chdir") as chdir,
            contextlib.redirect_stderr(stderr),
        ):
            code = self.adapter.resume_main(
                [store or self.store, str(self.work), self.spec.sid, native]
            )
        return code, execvpe, chdir, stderr.getvalue()

    def test_main_execs_the_stored_resume_with_the_saved_env(self):
        self.bound()
        with mock.patch.dict("os.environ", {"CALLER": "kept"}):
            code, execvpe, chdir, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))
        chdir.assert_called_once_with(str(self.cwd))
        name, argv, env = execvpe.call_args.args
        self.assertEqual(name, "codex")
        self.assertEqual((argv[:2], argv[2:6], argv[-1]), (("codex", "resume"), FLAGS, NATIVE))
        self.assertEqual({key: env[key] for key in SECRETS}, SECRETS)
        self.assertEqual((env["CODEX_HOME"], env["CALLER"]), (str(self.codex_home), "kept"))

    def test_saved_env_wins_and_local_git_names_and_defaults_are_not_added(self):
        self.bound()
        with mock.patch.dict("os.environ", {"SPEC_TOKEN": "caller", "NO_COLOR": "caller"}):
            _, execvpe, _, _ = self.run_main()
        env = execvpe.call_args.args[2]
        self.assertEqual((env["SPEC_TOKEN"], env["NO_COLOR"]), ("spec-3", "caller"))
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("CODEX_CI", env)

    def test_main_fails_closed(self):
        def malformed():
            self.bound()
            self.record().write_text("not json")

        cases = {
            "missing state": (lambda: None, NATIVE, "missing Codex recovery state for run ID"),
            "no native id": (self.prepare, NATIVE, "is bound to native ID None, not"),
            "other native id": (self.bound, CHILD, f"is bound to native ID '{NATIVE}', not"),
            "malformed state": (malformed, NATIVE, "malformed recovery state"),
        }
        for label, (arrange, native, message) in cases.items():
            with self.subTest(label):
                for path in Path(self.store).glob("*.json"):
                    path.unlink()
                arrange()
                code, execvpe, _, stderr = self.run_main(native)
                self.assertEqual(code, 1)
                execvpe.assert_not_called()
                self.assert_one_line(stderr, message)

    def test_a_relative_store_and_a_failed_exec_fail_closed(self):
        self.bound()
        code, execvpe, _, stderr = self.run_main(store="relative/store")
        self.assertEqual(code, 1)
        execvpe.assert_not_called()
        self.assert_one_line(stderr, "invalid Codex recovery store")
        stderr = io.StringIO()
        with (
            mock.patch("os.execvpe", side_effect=FileNotFoundError(2, "No such file", "codex")),
            mock.patch("os.chdir"),
            contextlib.redirect_stderr(stderr),
        ):
            code = self.adapter.resume_main([self.store, str(self.work), self.spec.sid, NATIVE])
        self.assertEqual(code, 1)
        self.assert_one_line(stderr.getvalue(), "No such file")

    def test_a_wrong_argument_count_fails_closed(self):
        self.bound()
        for argv in ([], [self.store, str(self.work), self.spec.sid]):
            stderr = io.StringIO()
            with (
                self.subTest(argv=argv),
                mock.patch("os.execvpe") as execvpe,
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(self.adapter.resume_main(argv), 1)
            execvpe.assert_not_called()
            self.assert_one_line(stderr.getvalue(), "expected STORE WORKDIR SID NATIVE")

    def assert_one_line(self, stderr, message):
        self.assertTrue(stderr.startswith("codex resume: "), stderr)
        self.assertEqual(stderr.count("\n"), 1, stderr)
        self.assertIn(message, stderr)
        for value in SECRETS.values():
            self.assertNotIn(value, stderr)


class ResumeAccessTest(Base):
    def test_resume_launch_needs_a_native_id(self):
        state = self.prepare()
        with self.assertRaisesRegex(ConfigError, "missing Codex native ID for run ID 'caller-id'"):
            self.common.resume_launch(state)

    def test_resume_launch_ignores_adapter_config(self):
        self.spec = dataclasses.replace(self.spec, access=Access(cwd=str(self.cwd)))
        self.prepare()
        state = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.assertEqual(
            CodexAdapter({}).common.resume_launch(state), self.common.resume_launch(state)
        )
