"""Driver validation behavior with caller-owned dependencies."""

import json
import os
import signal
import sys
import tempfile
import uuid
from unittest import mock

import agent_pm.session as session_pkg
from agent_pm.session.adapters import selection
from agent_pm.session.engine import api, records
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    DriverError,
)

from session.tests.case import Case
from session.tests.unit.driver.support import (
    LAUNCHED,
    SID,
    Base,
)


class ChecksTest(Base):
    def test_refuses_an_unknown_mode(self):
        for bad in ("pane", "", "Headless", None):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"mode must be one of headless, tui, in_process, got {bad!r}",
                    lambda *, bad=bad: self.start(mode=bad),
                )

    def test_refuses_an_idle_timeout_that_is_no_positive_number(self):
        for bad in (0, -1, 0.0, -0.5, float("nan"), True, False, "1"):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"idle_timeout must be a positive number of seconds, got {bad!r}",
                    lambda *, bad=bad: self.start(idle_timeout=bad),
                )

    def test_refuses_a_resume_that_is_no_session_id(self):
        for bad in ("", 1, b"x" * 100):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"resume must be a session id, got {bad!r:.80}",
                    lambda *, bad=bad: self.start(resume=bad),
                )

    def test_refuses_a_sid_that_is_not_a_non_empty_printable_string_without_a_slash(self):
        for bad, shown in (
            ("", "''"),
            ("a/b", "'a/b'"),
            ("a\tb", r"'a\tb'"),
            (5, "5"),
            ("s" * 200 + "\n", "'" + "s" * 79),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f'sid must be a non-empty printable string without "/", got {shown}',
                    lambda *, bad=bad: self.start(sid=bad),
                )

    def test_refuses_a_sid_other_than_resume(self):
        self.refused(
            ConfigError,
            "sid 's1' differs from resume 'r1'",
            lambda: self.start(sid="s1", resume="r1"),
        )

    def test_the_spec_sid_is_the_sessions_and_may_be_resumes(self):
        for kwargs in ({}, {"resume": "s1"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.start(spec=self.spec(sid="s1"), **kwargs).sid, "s1")

    def test_a_spec_name_is_tui_only(self):
        for runtime, mode in (("fake", "headless"), ("checked", "in_process")):
            with self.subTest(mode=mode):
                self.refused(
                    ConfigError,
                    "name: tui mode only",
                    lambda *, mode=mode, runtime=runtime: self.start(
                        runtime, mode, spec=self.spec(name="w1")
                    ),
                )

    def test_protect_is_tui_only(self):
        for runtime, mode in (("fake", "headless"), ("checked", "in_process")):
            with self.subTest(mode=mode):
                self.refused(
                    ConfigError,
                    "protect: tui mode only",
                    lambda *, mode=mode, runtime=runtime: self.start(
                        runtime, mode, spec=self.spec(protect=("/p",))
                    ),
                )

    def test_headless_flags_and_env_reach_the_adapter_and_child(self):
        LAUNCHED.clear()
        spec = self.spec(flags=("--custom", "value"), env={"FAKE_ENV": "spec"})
        handle, _ = self.run_once(runtime="env", spec=spec)
        self.assertEqual(
            (LAUNCHED[0].flags, dict(LAUNCHED[0].env), LAUNCHED[0].sid),
            (spec.flags, dict(spec.env), handle.sid),
        )
        with open(self.log, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["env"]["FAKE_ENV"], "spec")

    def test_session_selection_flags_are_refused_before_launch(self):
        from agent_pm.session.adapters.claude import ClaudeAdapter
        from agent_pm.session.adapters.codex import CodexAdapter

        for runtime, adapter in (("claude", ClaudeAdapter), ("codex", CodexAdapter)):
            self.configure(
                tables={runtime: {"implementation": f"adapters.{runtime}:{adapter.__name__}"}}
            )
            flags = (
                *adapter.forbidden_selection,
                *(f"{word}=x" for word in adapter.forbidden_selection if word.startswith("--")),
            )
            for flag in flags:
                with self.subTest(runtime=runtime, flag=flag):
                    self.refused(
                        ConfigError,
                        f"{flag}: picks the session; use sid= or resume=",
                        lambda *, flag=flag, runtime=runtime: self.start(
                            runtime=runtime, spec=self.spec(flags=("--verbose", flag, SID))
                        ),
                    )

    def test_selection_scanning_stops_at_the_separator_and_accepts_other_words(self):
        forbidden = ("--resume", "-r", "resume", "fork", "--last")
        for flags in (("--", *forbidden), ("--resumes", "--continued", "--teleport-x")):
            with self.subTest(flags=flags):
                selection.check(flags, forbidden)

    def test_selection_words_used_as_option_values_are_not_selectors(self):
        from agent_pm.session.adapters.codex import CodexAdapter

        for flags in (
            ("--add-dir", "fork"),
            ("--add-dir=resume",),
            ("--enable", "hooks"),
            ("-c", "model=x"),
            ("-cmodel=x",),
        ):
            with self.subTest(flags=flags):
                selection.check(flags, CodexAdapter.forbidden_selection, CodexAdapter.valued_flags)
        with self.assertRaisesRegex(ConfigError, "fork: picks the session"):
            selection.check(
                ("--add-dir", "safe", "fork"),
                CodexAdapter.forbidden_selection,
                CodexAdapter.valued_flags,
            )

    def test_selection_uses_declared_syntax_and_uuid_validation(self):
        syntax = (("--session-id",), ("--resume",))
        self.assertEqual(
            selection.select(SID, syntax, uuid=True, resume=False), ("--session-id", SID)
        )
        self.assertEqual(selection.select(SID, syntax, uuid=True, resume=True), ("--resume", SID))
        self.assertEqual(selection.select("native", ((), ("resume",)), resume=False), ())
        self.assertEqual(
            selection.select("native", ((), ("resume",)), resume=True), ("resume", "native")
        )
        for native in ("abc", SID.upper(), SID + "\n"):
            with (
                self.subTest(native=native),
                self.assertRaisesRegex(ConfigError, "session ids are UUIDs"),
            ):
                selection.select(native, syntax, uuid=True, resume=False)

    def test_a_registry_error_passes_through(self):
        self.refused(
            ConfigError,
            "no [adapters.nope] table in the configured adapters",
            lambda: self.start(runtime="nope"),
        )
        self.configure(model="m")
        self.refused(
            ConfigError, "unknown key 'model' in FakeAdapter's config", lambda: self.start()
        )

    def test_tui_on_a_runtime_without_it_is_unsupported(self):
        self.refused(
            ConfigError,
            "runtime 'fake' does not support tui mode",
            lambda: self.start(mode="tui"),
        )

    def test_headless_takes_no_name_or_events(self):
        self.refused(ConfigError, "name: tui mode only", lambda: self.start(name="w1"))
        self.refused(ConfigError, "events: tui mode only", lambda: self.start(events=self.scenario))

    def test_refuses_a_launch_without_a_command(self):
        self.refused(
            ConfigError, "runtime 'blank' gave no command", lambda: self.start(runtime="blank")
        )

    def test_an_exception_from_launch_propagates(self):
        self.refused(LookupError, "no binding", lambda: self.start(runtime="failing"))

    def test_a_failed_session_record_propagates_before_the_start(self):
        os.mkdir(self.workdir)
        target = os.path.join(self.dir, "elsewhere")
        os.symlink(target, records.path(self.workdir))
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen:
            with self.assertRaises(OSError):
                self.start()
        popen.assert_not_called()
        self.assertFalse(os.path.exists(target))


class SpawnTest(Base):
    def test_a_spawn_failure_is_a_driver_error_after_the_session_record(self):
        for error in (OSError("no exec"), ValueError("bad argument")):
            with self.subTest(error=error):
                self.workdir = os.path.join(self.dir, type(error).__name__)
                with mock.patch("subprocess.Popen", side_effect=error) as popen:
                    with self.assertRaises(DriverError) as caught:
                        self.start()
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                popen.assert_called_once()
                self.assertEqual([e["kind"] for e in self.written()], ["session"])


class SidTest(Base):
    def test_resume_continues_that_session(self):
        handle, argv = self.run_once(resume="r1")
        self.assertEqual(handle.sid, "r1")
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--sid") + 1], "r1")
        self.assertEqual(self.written()[0]["sid"], "r1")

    def test_sid_may_repeat_resume(self):
        handle, argv = self.run_once(sid="s1", resume="s1")
        self.assertEqual(handle.sid, "s1")
        self.assertIn("--resume", argv)

    def test_sid_names_a_new_session_and_reaches_launch(self):
        handle, argv = self.run_once(sid="s1")
        self.assertEqual((handle.sid, handle.info().sid), ("s1", "s1"))
        self.assertNotIn("--resume", argv)
        self.assertEqual(argv[argv.index("--sid") + 1], "s1")
        self.assertEqual(self.written()[0]["sid"], "s1")

    def test_neither_picks_a_uuid4(self):
        handle, argv = self.run_once()
        self.assertEqual(uuid.UUID(handle.sid).version, 4)
        self.assertEqual(str(uuid.UUID(handle.sid)), handle.sid)
        self.assertEqual(argv[argv.index("--sid") + 1], handle.sid)
        self.assertEqual(self.written()[0]["sid"], handle.sid)


class RuntimeNoteTest(Base):
    def test_with_commands_the_note_follows_the_prompt(self):
        _, argv = self.run_once(spec=self.spec(access=Access(commands=("git",))))
        self.assertEqual(argv[-1], "hello\n\nFAKE NOTE")

    def test_without_commands_the_prompt_is_unchanged(self):
        _, argv = self.run_once()
        self.assertEqual(argv[-1], "hello")

    def test_an_empty_note_leaves_the_prompt_unchanged(self):
        _, argv = self.run_once(runtime="quiet", spec=self.spec(access=Access(commands=("git",))))
        self.assertEqual(argv[-1], "hello")


class ExportsTest(Case):
    def test_api_exports_functions_and_constants_only(self):
        self.assertEqual(
            sorted(api.__all__),
            ["Driver", "Handle", "MODES", "SIGNALS", "holds", "note", "session_record"],
        )
        self.assertEqual([name for name in api.__all__ if not hasattr(api, name)], [])
        self.assertIs(api.session_record, records.session_record)

    def test_the_session_package_exports_them_not_the_module(self):
        for name in api.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(session_pkg, name), getattr(api, name))
                self.assertIn(name, session_pkg.__all__)
        self.assertNotIn("api", session_pkg.__all__)
        for name in session_pkg.__all__:
            if hasattr(engine_types, name):
                with self.subTest(name=name):
                    self.assertIs(getattr(session_pkg, name), getattr(engine_types, name))

    def test_constants(self):
        self.assertEqual(
            api.MODES,
            ("headless", "tui", "in_process"),
        )
        self.assertEqual((api.PROBE_DIR, api.PROBE_TIMEOUT), ("~/.agent-pm/probe", 120))
        self.assertEqual(api.SIGNALS, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))

    def test_note_appends_to_the_workdir_run_records(self):
        with tempfile.TemporaryDirectory() as d:
            api.note(d, "x", a=1)
            self.assertEqual(
                [(e["kind"], e["a"]) for e in records.read(records.path(d))], [("x", 1)]
            )
