"""adapters/base.py: spec types, sid_error, the frozen event set, Usage, Window and SessionStatus,
Adapter, Headless, Tui (hook kinds, labels, limit) and InProcess, through stub subclasses; nothing starts a process."""

import dataclasses
import inspect
import typing
import unittest
from types import MappingProxyType
from unittest import mock

from agent_pm.session.adapters import base
from agent_pm.session.adapters.base import (
    ACTIVITY,
    BLOCKED,
    TURN_END,
    Access,
    Activity,
    Adapter,
    Blocked,
    ConfigError,
    Exited,
    Headless,
    InProcess,
    Key,
    Launch,
    LimitHit,
    Paste,
    Reply,
    SessionBound,
    SessionStatus,
    Spec,
    Stderr,
    Tui,
    TurnEnd,
    Usage,
    Window,
)

from new.tests.case import Case


class StubAdapter(Adapter):
    keys = frozenset({"model", "flags"})

    def headless(self):
        return StubHeadless(self)

    def tui(self):
        return StubTui(self)


class StubHeadless(Headless):
    def launch(self, prompt, spec, resume):
        return Launch(("stub", prompt))

    def parse(self, record):
        return [Reply(text) for text in record.get("texts", [])]


class StubTui(Tui):
    def launch(self, prompt, spec, resume, hook):
        return Launch(("stub", *hook))

    def reply(self, payload):
        return payload.get("last", "")


class StubInProcess(InProcess):
    def run(self, prompt, spec, resume):
        yield Reply(prompt)


class CheckOnlyInProcess(InProcess):
    def check(self, prompt, spec, resume):
        return None


class LabeledTui(StubTui):
    hooks = {"end": TURN_END, "wait": BLOCKED, "work": ACTIVITY}


class DetailedTui(LabeledTui):
    def pending(self, payload):
        return len(payload.get("tasks", []))

    def activity(self, payload):
        return payload.get("doing", "")

    def blocked_reason(self, payload):
        return payload.get("why", "")


class NativeIdTui(LabeledTui):
    def session_id(self, payload):
        return payload.get("thread", "")


class Refusals(Case):
    def refused(self, build, message):
        with self.assertRaises(ConfigError) as caught:
            build()
        self.assertEqual(str(caught.exception), message)


class AccessTest(Refusals):
    def test_defaults(self):
        access = Access()
        self.assertEqual(
            (access.dirs, access.commands, access.cwd, access.project, access.reads, access.env),
            ((), (), "", False, (), {}),
        )

    def test_accepts_tuples_of_strings(self):
        access = Access(dirs=("/a",), commands=("git",), cwd="/c", project=True, reads=("/r",))
        self.assertEqual((access.dirs, access.commands, access.reads), (("/a",), ("git",), ("/r",)))

    def test_keeps_reads_and_env_together(self):
        access = Access(reads=("/r",), env={"A": "b"})
        self.assertEqual((access.reads, access.env), (("/r",), {"A": "b"}))

    def test_takes_keywords_only(self):
        with self.assertRaises(TypeError):
            Access(("/a",), ("git",), "/c", True, ("/r",))

    def test_refuses_dirs_commands_or_reads_that_are_not_a_tuple_of_strings(self):
        for bad in (["/a"], ("/a", 1), "/a", None):
            with self.subTest(bad=bad):
                self.refused(lambda: Access(dirs=bad), "dirs must be a tuple of strings")
                self.refused(lambda: Access(commands=bad), "commands must be a tuple of strings")
                self.refused(lambda: Access(reads=bad), "reads must be a tuple of strings")

    def test_accepts_an_env_of_shell_names_to_strings(self):
        env = {"PYTHONPATH": "/a b;c", "_x1": ""}
        self.assertEqual(Access(env=MappingProxyType(env)).env, env)

    def test_refuses_an_env_name_a_shell_assignment_cannot_hold_or_a_value_not_a_string_without_nul(
        self,
    ):
        message = "access env must map names ([A-Za-z_][A-Za-z0-9_]*) to strings without NUL"
        for bad in (
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
        ):
            with self.subTest(bad=bad):
                self.refused(lambda: Access(env=bad), message)


class SpecTest(Refusals):
    def test_defaults(self):
        spec = Spec(workdir="/w")
        self.assertEqual(
            (spec.access, spec.tier, spec.effort, spec.sid), (Access(), None, None, None)
        )
        self.assertEqual(
            (spec.flags, spec.env, spec.name, spec.protect),
            ((), {}, None, ()),
        )

    def plain(self, **fields):
        return Spec(
            workdir="/w",
            flags=("--model", "x", "--", "-p"),
            env={"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"},
            name="worker-1",
            protect=("/p", "/q r/ü"),
            **fields,
        )

    def test_accepts_the_plain_session_fields(self):
        spec = self.plain()
        self.assertEqual(
            (spec.flags, dict(spec.env), spec.name, spec.protect),
            (
                ("--model", "x", "--", "-p"),
                {"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"},
                "worker-1",
                ("/p", "/q r/ü"),
            ),
        )
        self.assertEqual(Spec(workdir="/w", env=MappingProxyType({"A": "1"})).env, {"A": "1"})
        self.assertEqual(Spec(workdir="/w", name="").name, "")

    def test_env_is_an_immutable_copy_and_specs_compare_by_value(self):
        source = {"A": "1"}
        spec = Spec(workdir="/w", env=source)
        source["B"] = "2"
        self.assertEqual(dict(spec.env), {"A": "1"})
        with self.assertRaises(TypeError):
            spec.env["C"] = "3"
        self.assertEqual(spec, Spec(workdir="/w", env={"A": "1"}))
        self.assertNotEqual(spec, Spec(workdir="/w", env={"A": "2"}))
        self.assertEqual(self.plain(), self.plain())

    def test_replace_keeps_the_plain_session_fields(self):
        spec = dataclasses.replace(self.plain(), tier=2, sid="s")
        self.assertEqual(spec, self.plain(tier=2, sid="s"))
        self.assertEqual(dict(spec.env), {"A": "1", "_b2": "", "PATH": "/bin:/usr/bin"})

    def test_refuses_flags_that_are_not_a_tuple_of_strings_without_nul(self):
        for bad in (["-x"], ("-x\0",), ("-x", 1), "-x", None):
            with self.subTest(bad=bad):
                self.refused(
                    lambda: Spec(workdir="/w", flags=bad),
                    "flags must be a tuple of strings without NUL",
                )

    def test_refuses_env_that_does_not_map_variable_names_to_strings_without_nul(self):
        for bad in (
            {"A": 1},
            {"A": None},
            {"A": "b\0"},
            {1: "a"},
            {"": "a"},
            {"1A": "a"},
            {"A-B": "a"},
            {"A B": "a"},
            {"A=": "a"},
            {"A\n": "a"},
            {"A\0": "a"},
            {"Ä": "a"},
            [("A", "b")],
            "A=b",
            None,
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda: Spec(workdir="/w", env=bad),
                    "env must map variable names to strings without NUL",
                )

    def test_refuses_a_name_other_than_none_or_a_string(self):
        for bad in (5, b"w", ("w",)):
            with self.subTest(bad=bad):
                self.refused(lambda: Spec(workdir="/w", name=bad), "name must be None or a string")

    def test_refuses_protect_that_is_not_a_tuple_of_printable_absolute_paths(self):
        for bad in (["/p"], ("p",), ("",), ("/p\n",), ("/p\0",), ("/p", 1), (b"/p",), "/p", None):
            with self.subTest(bad=bad):
                self.refused(
                    lambda: Spec(workdir="/w", protect=bad),
                    "protect must be a tuple of printable absolute paths",
                )

    def test_keyword_only_and_frozen(self):
        with self.assertRaises(TypeError):
            Spec("/w")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Spec(workdir="/w").sid = "s"

    def test_accepts_every_tier_and_effort(self):
        self.assertEqual(base.EFFORTS, ("low", "medium", "high", "xhigh", "max"))
        for tier in (1, 2, 3, 4):
            for effort in base.EFFORTS:
                spec = Spec(
                    workdir="/w", access=Access(cwd="/c"), tier=tier, effort=effort, sid="s-1"
                )
                self.assertEqual((spec.tier, spec.effort), (tier, effort))

    def test_refuses_a_workdir_that_is_not_a_printable_absolute_path(self):
        for bad, shown in (
            ("w", "'w'"),
            ("", "''"),
            (5, "5"),
            (None, "None"),
            ("/w\n", r"'/w\n'"),
            ("/w\0", r"'/w\x00'"),
            (b"/w", "b'/w'"),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda: Spec(workdir=bad),
                    f"workdir must be a printable absolute path, got {shown}",
                )

    def test_shows_at_most_80_characters_of_a_bad_workdir(self):
        self.refused(
            lambda: Spec(workdir="/" + "x" * 200 + "\n"),
            "workdir must be a printable absolute path, got '/" + "x" * 78,
        )

    def test_refuses_a_tier_other_than_an_integer_1_to_4(self):
        for bad, shown in ((0, "0"), (5, "5"), (True, "True"), ("2", "'2'"), (2.0, "2.0")):
            with self.subTest(bad=bad):
                self.refused(
                    lambda: Spec(workdir="/w", tier=bad),
                    f"tier must be an integer 1–4, got {shown}",
                )

    def test_refuses_an_effort_outside_efforts(self):
        for bad, shown in (("extreme", "'extreme'"), ("", "''"), ("LOW", "'LOW'"), (3, "3")):
            with self.subTest(bad=bad):
                self.refused(
                    lambda: Spec(workdir="/w", effort=bad),
                    f"effort must be one of low, medium, high, xhigh, max, got {shown}",
                )

    def test_refuses_an_access_cwd_that_is_not_a_printable_absolute_path(self):
        for bad, shown in (("rel", "'rel'"), ("/c\0", r"'/c\x00'")):
            with self.subTest(bad=bad):
                self.refused(
                    lambda: Spec(workdir="/w", access=Access(cwd=bad)),
                    f"cwd must be a printable absolute path, got {shown}",
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
                    lambda: Spec(workdir="/w", sid=bad),
                    f'sid must be a non-empty printable string without "/", got {shown}',
                )


class SidErrorTest(Case):
    def test_none_for_a_non_empty_printable_string_without_a_slash(self):
        for sid in ("s", "s-1", "0b6c8f9e-2a1d-4c3e-9f7a-5b4d3c2e1f00", "a b", "ü"):
            with self.subTest(sid=sid):
                self.assertIsNone(base.sid_error(sid))

    def test_the_error_text_otherwise(self):
        for bad, shown in (
            ("", "''"),
            ("a/b", "'a/b'"),
            ("a\tb", r"'a\tb'"),
            (5, "5"),
            (None, "None"),
            ("s" * 200 + "\n", "'" + "s" * 79),
        ):
            with self.subTest(bad=bad):
                self.assertEqual(
                    base.sid_error(bad),
                    f'sid must be a non-empty printable string without "/", got {shown}',
                )


class LaunchTest(Refusals):
    def test_defaults(self):
        launch = Launch(("x",))
        self.assertEqual(
            (launch.argv, launch.env, launch.unset, launch.cwd, launch.transcript, launch.resume),
            (("x",), {}, (), "", "", ""),
        )

    def test_project_override_is_optional_and_boolean(self):
        self.assertIsNone(Launch(("x",)).project)
        for value in (False, True):
            self.assertIs(Launch(("x",), project=value).project, value)
        for value in (0, 1, "yes", []):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "launch: project"):
                Launch(("x",), project=value)

    def test_accepts_strings(self):
        launch = Launch(
            ("x", "-y"),
            env=MappingProxyType({"A": "b"}),
            unset=("C",),
            cwd="/c",
            transcript="/t",
            resume="x --resume s",
        )
        self.assertEqual((launch.env["A"], launch.unset), ("b", ("C",)))

    def test_refuses_argv_env_or_unset_that_is_not_strings_without_nul(self):
        for field, bads in (
            ("argv", (["x"], ("x\0",), ("x", 1), "x")),
            ("env", ({"A": 1}, {1: "a"}, {"A\0": "b"}, {"A": "b\0"}, [("A", "b")])),
            ("unset", (["A"], ("A\0",), ("A", 2))),
        ):
            for bad in bads:
                with self.subTest(field=field, bad=bad):
                    fields = {"argv": ("x",), field: bad}
                    self.refused(
                        lambda: Launch(**fields), f"launch: {field} must be strings without NUL"
                    )


class EventsTest(Case):
    def test_the_frozen_set_is_the_seven_events(self):
        self.assertEqual(
            typing.get_args(base.Event),
            (SessionBound, Activity, Reply, TurnEnd, Blocked, LimitHit, Exited),
        )
        self.assertNotIn(Stderr, typing.get_args(base.Event))

    def test_defaults(self):
        self.assertEqual(
            (Activity(), TurnEnd(), Blocked(), LimitHit()),
            (Activity(""), TurnEnd("", 0), Blocked(""), LimitHit(None)),
        )
        self.assertEqual(LimitHit(1767225600.0).resets_at, 1767225600.0)
        for event, name in ((TurnEnd(), "reply"), (LimitHit(), "resets_at")):
            with self.subTest(name=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(event, name, "x")


class ReportsTest(Case):
    def test_usage_window_and_session_status_are_frozen_records(self):
        window = Window(used_percent=120.5, resets_at=None)
        self.assertEqual((window.used_percent, window.resets_at), (120.5, None))
        usage = Usage(five_hour=window, weekly=None)
        self.assertEqual((usage.five_hour, usage.weekly), (Window(120.5, None), None))
        self.assertEqual(Usage(None, Window(3.0, 1.5)).weekly.resets_at, 1.5)
        status = SessionStatus(resumable="yes", last_active=1.5, resume_command="x --resume s")
        self.assertEqual(
            (status.resumable, status.last_active, status.resume_command),
            ("yes", 1.5, "x --resume s"),
        )
        self.assertEqual(base.RESUMABLE, ("yes", "no", "unknown"))
        for record, name in ((window, "used_percent"), (usage, "five_hour"), (status, "resumable")):
            with self.subTest(name=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, name, "x")


class AdapterTest(Refusals):
    def test_the_abstract_classes_cannot_be_built(self):
        with self.assertRaises(TypeError):
            Adapter({})
        with self.assertRaises(TypeError):
            Headless(StubAdapter({}))
        with self.assertRaises(TypeError):
            Tui(StubAdapter({}))
        with self.assertRaises(TypeError):
            InProcess(StubAdapter({}))

    def test_refuses_the_first_unknown_key_in_sorted_order(self):
        self.refused(
            lambda: StubAdapter({"zeta": 1, "alpha": 2, "model": "m"}),
            "unknown key 'alpha' in StubAdapter's config",
        )

    def test_selects_a_session_only_through_the_selector_the_driver_configured(self):
        adapter = StubAdapter({})
        for resume in (False, True):
            self.refused(
                lambda: adapter.select_session("n", resume),
                "session selector is not configured by Driver",
            )
        adapter.configure_selector(lambda native, resume: ("--pick", native, str(resume)))
        self.assertEqual(adapter.select_session("n", True), ("--pick", "n", "True"))

    def test_keeps_a_copy_of_its_config(self):
        config = {"model": "m", "flags": ["-x"]}
        adapter = StubAdapter(config)
        self.assertEqual(adapter.config, config)
        self.assertIsNot(adapter.config, config)

    def test_defaults(self):
        self.assertEqual((Adapter.keys, Adapter.runtime_note, Tui.dialogs), (frozenset(), "", ()))
        self.assertEqual(
            (Adapter.session_env, Adapter.identity_env, Adapter.child_defaults), ("", (), {})
        )
        adapter = StubAdapter({})
        self.assertIsNone(adapter.usage_probe())
        for output in ("", "garbage", '{"type": "rate_limit_event"}'):
            with self.subTest(output=output):
                self.assertIsNone(adapter.usage(output))
        self.assertEqual(adapter.session_status("s", "/c"), SessionStatus("unknown", None, ""))

    def test_session_metadata_preserves_info_by_default(self):
        info = base.SessionInfo("s", "n", "/c", False, "/t", "resume n")
        self.assertIs(StubAdapter({}).session_metadata(info), info)

    def test_writable_dirs_are_none_unless_overridden(self):
        spec = Spec(workdir="/w", access=Access(dirs=("/d",), cwd="/c"), sid="s")
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertEqual(StubAdapter({}).writable_dirs(spec, resume), ())

    def test_in_process_is_none_unless_overridden(self):
        self.assertEqual(Adapter.__abstractmethods__, frozenset({"headless", "tui"}))
        self.assertIsNone(StubAdapter({}).in_process())

    def test_runs_keep_their_adapter(self):
        adapter = StubAdapter({})
        self.assertIs(adapter.headless().adapter, adapter)
        self.assertIs(adapter.tui().adapter, adapter)


class HeadlessTest(Case):
    def test_events_parses_each_json_object_line_in_order_and_skips_the_rest(self):
        lines = [
            '{"texts": ["a"]}\n',
            "not json\n",
            "[1, 2]\n",
            "\n",
            "",
            '"text"\n',
            "null\n",
            "[" * 100000 + "]" * 100000 + "\n",
            "{}\n",
            '{"texts": ["b", "c"]}',
        ]
        self.assertEqual(
            list(StubHeadless(StubAdapter({})).events(lines)), [Reply("a"), Reply("b"), Reply("c")]
        )


class InProcessTest(Case):
    def test_run_is_its_only_abstract_method(self):
        self.assertEqual(InProcess.__abstractmethods__, frozenset({"run"}))
        with self.assertRaises(TypeError):
            CheckOnlyInProcess(StubAdapter({}))

    def test_keeps_its_adapter_and_checks_nothing_by_default(self):
        adapter = StubAdapter({})
        run = StubInProcess(adapter)
        self.assertIs(run.adapter, adapter)
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertIsNone(run.check("p", Spec(workdir="/w", sid="s"), resume))


class TuiTest(Case):
    def setUp(self):
        self.run = DetailedTui(StubAdapter({}))
        self.end = {"session_id": "n1", "last": "done", "tasks": [1, 2]}

    def test_the_hook_kinds(self):
        self.assertEqual((TURN_END, BLOCKED, ACTIVITY), ("turn_end", "blocked", "activity"))

    def test_defaults(self):
        run = StubTui(StubAdapter({}))
        self.assertEqual(Tui.hooks, {})
        self.assertEqual(
            (run.pending(self.end), run.activity({"doing": "x"}), run.blocked_reason({"why": "y"})),
            (0, "", ""),
        )
        for screen in ("", "limit reached\n> "):
            with self.subTest(screen=screen):
                self.assertIsNone(run.limit(screen))

    def test_submit_pastes_then_presses_enter(self):
        self.assertEqual(self.run.submit("x"), (Paste("x"), Key("Enter")))

    def test_session_id_is_the_payloads_session_id_when_a_str(self):
        for payload, native in (
            ({"session_id": "n1"}, "n1"),
            ({"session_id": ""}, ""),
            ({"session_id": 5}, ""),
            ({"session_id": None}, ""),
            ({}, ""),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.run.session_id(payload), native)

    def test_a_new_session_id_binds_it_first(self):
        self.assertEqual(
            self.run.hook_events("end", self.end), [SessionBound("n1"), TurnEnd("done", 2)]
        )
        self.assertEqual(
            self.run.hook_events("end", self.end, bound="n0"),
            [SessionBound("n1"), TurnEnd("done", 2)],
        )

    def test_the_bound_or_no_session_id_binds_nothing(self):
        self.assertEqual(self.run.hook_events("end", self.end, bound="n1"), [TurnEnd("done", 2)])
        for session in ({}, {"session_id": ""}, {"session_id": 5}):
            with self.subTest(session=session):
                self.assertEqual(
                    self.run.hook_events("end", {"last": "r", **session}), [TurnEnd("r", 0)]
                )

    def test_each_label_gives_the_event_of_its_kind(self):
        payload = {"session_id": "n1", "last": "r", "tasks": [1], "doing": "Bash", "why": "ask"}
        for label, event in (
            ("end", TurnEnd("r", 1)),
            ("wait", Blocked("ask")),
            ("work", Activity("Bash")),
        ):
            with self.subTest(label=label):
                self.assertEqual(self.run.hook_events(label, payload, bound="n1"), [event])

    def test_the_kinds_default_details(self):
        run = LabeledTui(StubAdapter({}))
        payload = {"last": "r", "tasks": [1], "doing": "Bash", "why": "ask"}
        for label, event in (
            ("end", TurnEnd("r", 0)),
            ("wait", Blocked("")),
            ("work", Activity("")),
        ):
            with self.subTest(label=label):
                self.assertEqual(run.hook_events(label, payload), [event])

    def test_an_unknown_label_gives_nothing_more(self):
        for label in ("other", "", "End", TURN_END, "Stop", "PostToolUse"):
            with self.subTest(label=label):
                self.assertEqual(
                    self.run.hook_events(label, {"session_id": "n1", "last": "r"}, bound="n1"), []
                )
        self.assertEqual(self.run.hook_events("other", {"session_id": "n1"}), [SessionBound("n1")])

    def test_without_hooks_no_label_has_a_kind(self):
        run = StubTui(StubAdapter({}))
        payload = {"session_id": "n1", "last": "r", "tool_name": "Bash"}
        for label in ("Stop", "PostToolUse", "Notification", "PermissionRequest", "end"):
            with self.subTest(label=label):
                self.assertEqual(run.hook_events(label, payload), [SessionBound("n1")])

    def test_base_names_no_runtime_hook(self):
        source = inspect.getsource(base)
        for name in (
            "Stop",
            "PreToolUse",
            "PostToolUse",
            "Notification",
            "PermissionRequest",
            "UserPromptSubmit",
        ):
            with self.subTest(name=name):
                self.assertNotRegex(source, rf"\b{name}\b")

    def test_hook_events_binds_the_id_session_id_reads(self):
        run = NativeIdTui(StubAdapter({}))
        self.assertEqual(
            run.hook_events("end", {"thread": "t1", "session_id": "n1", "last": "r"}),
            [SessionBound("t1"), TurnEnd("r", 0)],
        )
        self.assertEqual(
            run.hook_events("end", {"thread": "t1", "last": "r"}, bound="t1"), [TurnEnd("r", 0)]
        )
        self.assertEqual(run.hook_events("work", {"session_id": "n1"}), [Activity("")])


class NoProcessTest(Case):
    def test_nothing_starts_a_process(self):
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError("Popen")) as popen,
            mock.patch("subprocess.run", side_effect=AssertionError("run")) as run,
        ):
            adapter = StubAdapter({"model": "m"})
            tui = adapter.tui()
            tui.submit("x")
            tui.hook_events("end", {"session_id": "n1"})
            labeled = DetailedTui(adapter)
            for label in labeled.hooks:
                labeled.hook_events(label, {"session_id": "n1", "doing": "Bash"}, bound="n1")
            tui.limit("screen")
            list(adapter.headless().events(['{"texts": ["a"]}']))
            adapter.usage_probe()
            adapter.usage("output")
            adapter.session_status("s", "/c")
            adapter.in_process()
            StubInProcess(adapter).check("p", Spec(workdir="/w"), False)
        popen.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
