"""Claude claude behavior."""

import os
import shlex

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.claude.common import ClaudeCommon as UnconfiguredCommon
from agent_pm.session.adapters.claude.headless import ClaudeHeadless
from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    Launch,
    SessionInfo,
    SessionStatus,
    Usage,
    Window,
)

from session.tests.unit.adapters.claude.support import (
    CEILING,
    CONFIG,
    NOTE,
    PROBE,
    SID,
    UNSET,
    WHAT,
    Base,
    ClaudeCommon,
    Spec,
    event,
)


class ConfigTest(Base):
    def test_every_key_is_optional_and_each_is_accepted(self):
        self.assertEqual(ClaudeAdapter({}).config, {})
        self.assertEqual(ClaudeAdapter(CONFIG).config, CONFIG)

    def test_an_unknown_key_is_refused(self):
        self.refused(
            lambda: ClaudeAdapter({"model": "m"}),
            "unknown key 'model' in ClaudeAdapter's config",
        )

    def test_a_wrong_type_names_the_key_never_the_value(self):
        rows = (
            ("flags", "SECRET"),
            ("flags", ["--x", 1]),
            ("flags", ("SECRET",)),
            ("flags", {"SECRET": "x"}),
            ("allow", "SECRET"),
            ("allow", [None]),
            ("tiers", "SECRET"),
            ("tiers", ["SECRET"]),
            ("tiers", {"1": 4}),
            ("tiers", {"5": "SECRET"}),
            ("tiers", {"0": "SECRET"}),
            ("tiers", {1: "SECRET"}),
            ("tiers", {"1": " "}),
            ("efforts", {"low": "a\nb"}),
            ("efforts", "SECRET"),
            ("efforts", {"low": 1}),
            ("efforts", {"turbo": "SECRET"}),
            ("efforts", {1: "SECRET"}),
            ("env", "SECRET"),
            ("env", {"K": 1}),
            ("env", {1: "SECRET"}),
            ("env", ["SECRET"]),
        )
        for key, value in rows:
            with self.subTest(key=key, value=value):
                self.refused(
                    lambda *, key=key, value=value: ClaudeAdapter({key: value}),
                    f"[adapters.claude].{key} must be {WHAT[key]}",
                )

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
                    ClaudeAdapter({"env": {key: value}})
                self.assertIn("[adapters.claude].env", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))

    def test_setting_sources_in_flags_is_refused(self):
        for flags in (
            ["--setting-sources"],
            ["--setting-sources=user"],
            ["--verbose", "--setting-sources", "user"],
        ):
            with self.subTest(flags=flags):
                self.refused(
                    lambda *, flags=flags: ClaudeAdapter({"flags": flags}),
                    "--setting-sources is set by the Claude adapter: remove it from [adapters.claude].flags",
                )

    def test_the_env_attributes(self):
        adapter = ClaudeAdapter({})
        self.assertEqual(
            (adapter.session_env, adapter.identity_env, adapter.child_defaults),
            ("CLAUDECODE", UNSET, {}),
        )


class AdapterTest(Base):
    def test_its_parts(self):
        adapter = ClaudeAdapter(CONFIG)
        self.assertEqual(
            ClaudeAdapter.keys, frozenset({"flags", "allow", "tiers", "efforts", "env"})
        )
        self.assertEqual((ClaudeAdapter.runtime_note, ClaudeCommon.NOTE), (NOTE, NOTE))
        self.assertIsInstance(adapter.common, UnconfiguredCommon)
        for mode, kind in ((adapter.headless, ClaudeHeadless), (adapter.tui, ClaudeTui)):
            with self.subTest(kind=kind):
                self.assertIs(type(mode()), kind)
                self.assertIsNot(mode(), mode())
                self.assertIs(mode().common, adapter.common)

    def test_the_usage_probe_and_its_reading(self):
        self.assertEqual(ClaudeCommon.PROBE, PROBE)
        self.assertEqual(
            ClaudeAdapter(CONFIG).usage_probe(),
            Launch(PROBE, env={**CEILING, "K": "v"}, unset=UNSET),
        )
        self.assertEqual(ClaudeAdapter({}).usage(event(five=0.25)), Usage(Window(25.0, 1.0), None))
        self.assertIsNone(ClaudeAdapter({}).usage("garbage"))

    def test_status_uses_an_in_store_recorded_transcript_and_rebuilds_resume(self):
        adapter = ClaudeAdapter({})
        folder = os.path.dirname(adapter.common.transcript(self.work, SID))
        os.makedirs(folder)
        path = os.path.join(folder, "historical.jsonl")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("recorded")
        info = SessionInfo(
            sid=SID, cwd=self.work, project=False, transcript=path, resume="rm -rf ~"
        )
        self.assertEqual(
            adapter.session_status(info),
            SessionStatus(
                "yes",
                os.stat(path).st_mtime,
                f"cd {shlex.quote(self.work)} && claude --resume {SID}",
            ),
        )

    def test_session_status(self):
        self.assertEqual(
            ClaudeAdapter({}).session_status(
                SessionInfo(sid=SID, cwd=self.work, project=False, transcript="", resume="")
            ),
            SessionStatus("no", None, f"cd {shlex.quote(self.work)} && claude --resume {SID}"),
        )
        self.assertEqual(
            ClaudeAdapter({}).session_status(
                SessionInfo(sid="x", cwd=self.work, project=False, transcript="", resume="")
            ),
            SessionStatus("no", None, ""),
        )


class WritableDirsTest(Base):
    def setUp(self):
        super().setUp()
        self.cwd, self.added = (os.path.join(self.tmp, name) for name in ("cwd", "added"))
        for path in (self.cwd, self.added):
            os.mkdir(path)
        self.link = os.path.join(self.tmp, "link")
        os.symlink(self.added, self.link)

    def dirs(self, *, resume=False, workdir=None, **access) -> tuple[str, ...]:
        spec = Spec(workdir=workdir or self.work, sid=SID, access=Access(**access), resume=resume)
        return ClaudeAdapter(CONFIG).writable_dirs(spec)

    def test_the_workdir_without_a_cwd_then_the_added_dirs(self):
        self.assertEqual(self.dirs(), (self.work,))
        self.assertEqual(self.dirs(dirs=(self.added, self.cwd)), (self.work, self.added, self.cwd))

    def test_the_cwd_first_when_set(self):
        self.assertEqual(self.dirs(cwd=self.cwd), (self.cwd,))
        self.assertEqual(self.dirs(cwd=self.cwd, dirs=(self.added,)), (self.cwd, self.added))

    def test_real_paths_each_once_in_order(self):
        work_link = os.path.join(self.tmp, "work-link")
        os.symlink(self.work, work_link)
        self.assertEqual(self.dirs(workdir=work_link), (self.work,))
        self.assertEqual(
            self.dirs(cwd=self.link, dirs=(self.added, self.work, work_link, self.cwd + "/")),
            (self.added, self.work, self.cwd),
        )

    def test_a_resume_the_same(self):
        for access, dirs in (
            ({}, (self.work,)),
            ({"cwd": self.cwd}, (self.cwd,)),
            ({"dirs": (self.link, self.work)}, (self.work, self.added)),
        ):
            with self.subTest(access=access):
                self.assertEqual(self.dirs(resume=True, **access), dirs)

    def test_read_dirs_never(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertEqual(
                    self.dirs(resume=resume, dirs=(self.added,), reads=(self.cwd, self.link)),
                    (self.work, self.added),
                )

    def test_access_and_flag_dirs_are_combined_for_every_session(self):
        os.mkdir(os.path.join(self.cwd, "sub"))
        flags = (
            "--verbose",
            "--add-dir",
            "sub",
            "--add-dir=../added",
            "--add-dir",
            self.link,
            "--",
            "--add-dir",
            "/x",
        )
        for resume in (False, True):
            for cwd, given, dirs in (
                ("", (), (self.work, self.tmp)),
                (self.cwd, (), (self.cwd, self.tmp)),
                (self.cwd, flags, (self.cwd, self.tmp, os.path.join(self.cwd, "sub"), self.added)),
            ):
                with self.subTest(resume=resume, cwd=cwd, flags=given):
                    spec = Spec(
                        workdir=self.work,
                        sid=SID,
                        access=Access(cwd=cwd, dirs=(self.tmp,), reads=(self.tmp,)),
                        flags=given,
                        resume=resume,
                    )
                    self.assertEqual(ClaudeAdapter(CONFIG).writable_dirs(spec), dirs)
