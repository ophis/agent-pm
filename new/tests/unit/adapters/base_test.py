"""new/core/src/adapters/base.py: spec types, sid_error, the frozen event set, Usage and SessionStatus, Adapter,
HeadlessRun and TuiRun, through stub subclasses; nothing starts a process."""
import dataclasses
import typing
import unittest
from types import MappingProxyType
from unittest import mock

from new.tests import hermetic  # noqa: F401
from adapters import base
from adapters.base import (Access, Activity, Adapter, Blocked, ConfigError, Exited, HeadlessRun, Key, Launch,
                           LimitHit, Paste, Reply, SessionBound, SessionStatus, Spec, Stderr, TuiRun, TurnEnd, Usage)


class StubAdapter(Adapter):
    keys = frozenset({"model", "flags"})

    def headless_run(self):
        return StubHeadlessRun(self)

    def tui_run(self):
        return StubTuiRun(self)


class StubHeadlessRun(HeadlessRun):
    def launch(self, prompt, spec, resume):
        return Launch(("stub", prompt))

    def parse(self, record):
        return [Reply(text) for text in record.get("texts", [])]


class StubTuiRun(TuiRun):
    def launch(self, prompt, spec, resume, hook):
        return Launch(("stub", *hook))

    def reply(self, payload):
        return payload.get("last", "")


class CountingTuiRun(StubTuiRun):
    def pending(self, payload):
        return len(payload.get("tasks", []))


class Refusals(unittest.TestCase):
    def refused(self, build, message):
        with self.assertRaises(ConfigError) as caught:
            build()
        self.assertEqual(str(caught.exception), message)


class AccessTest(Refusals):
    def test_defaults(self):
        access = Access()
        self.assertEqual((access.dirs, access.commands, access.cwd, access.project), ((), (), "", False))

    def test_accepts_tuples_of_strings(self):
        access = Access(dirs=("/a",), commands=("git",), cwd="/c", project=True)
        self.assertEqual((access.dirs, access.commands), (("/a",), ("git",)))

    def test_refuses_dirs_or_commands_that_are_not_a_tuple_of_strings(self):
        for bad in (["/a"], ("/a", 1), "/a", None):
            with self.subTest(bad=bad):
                self.refused(lambda: Access(dirs=bad), "dirs must be a tuple of strings")
                self.refused(lambda: Access(commands=bad), "commands must be a tuple of strings")


class SpecTest(Refusals):
    def test_defaults(self):
        spec = Spec(workdir="/w")
        self.assertEqual((spec.access, spec.tier, spec.effort, spec.sid), (Access(), None, None, None))

    def test_keyword_only_and_frozen(self):
        with self.assertRaises(TypeError):
            Spec("/w")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Spec(workdir="/w").sid = "s"

    def test_accepts_every_tier_and_effort(self):
        self.assertEqual(base.EFFORTS, ("low", "medium", "high", "xhigh", "max"))
        for tier in (1, 2, 3, 4):
            for effort in base.EFFORTS:
                spec = Spec(workdir="/w", access=Access(cwd="/c"), tier=tier, effort=effort, sid="s-1")
                self.assertEqual((spec.tier, spec.effort), (tier, effort))

    def test_refuses_a_workdir_that_is_not_a_printable_absolute_path(self):
        for bad, shown in (("w", "'w'"), ("", "''"), (5, "5"), (None, "None"), ("/w\n", r"'/w\n'"),
                           ("/w\0", r"'/w\x00'"), (b"/w", "b'/w'")):
            with self.subTest(bad=bad):
                self.refused(lambda: Spec(workdir=bad), f"workdir must be a printable absolute path, got {shown}")

    def test_shows_at_most_80_characters_of_a_bad_workdir(self):
        self.refused(lambda: Spec(workdir="/" + "x" * 200 + "\n"),
                     "workdir must be a printable absolute path, got '/" + "x" * 78)

    def test_refuses_a_tier_other_than_an_integer_1_to_4(self):
        for bad, shown in ((0, "0"), (5, "5"), (True, "True"), ("2", "'2'"), (2.0, "2.0")):
            with self.subTest(bad=bad):
                self.refused(lambda: Spec(workdir="/w", tier=bad), f"tier must be an integer 1–4, got {shown}")

    def test_refuses_an_effort_outside_efforts(self):
        for bad, shown in (("extreme", "'extreme'"), ("", "''"), ("LOW", "'LOW'"), (3, "3")):
            with self.subTest(bad=bad):
                self.refused(lambda: Spec(workdir="/w", effort=bad),
                             f"effort must be one of low, medium, high, xhigh, max, got {shown}")

    def test_refuses_an_access_cwd_that_is_not_a_printable_absolute_path(self):
        for bad, shown in (("rel", "'rel'"), ("/c\0", r"'/c\x00'")):
            with self.subTest(bad=bad):
                self.refused(lambda: Spec(workdir="/w", access=Access(cwd=bad)),
                             f"cwd must be a printable absolute path, got {shown}")

    def test_refuses_a_sid_that_is_not_a_non_empty_printable_string_without_a_slash(self):
        for bad, shown in (("", "''"), ("a/b", "'a/b'"), ("a\tb", r"'a\tb'"), (5, "5"),
                           ("s" * 200 + "\n", "'" + "s" * 79)):
            with self.subTest(bad=bad):
                self.refused(lambda: Spec(workdir="/w", sid=bad),
                             f'sid must be a non-empty printable string without "/", got {shown}')


class SidErrorTest(unittest.TestCase):
    def test_none_for_a_non_empty_printable_string_without_a_slash(self):
        for sid in ("s", "s-1", "0b6c8f9e-2a1d-4c3e-9f7a-5b4d3c2e1f00", "a b", "ü"):
            with self.subTest(sid=sid):
                self.assertIsNone(base.sid_error(sid))

    def test_the_error_text_otherwise(self):
        for bad, shown in (("", "''"), ("a/b", "'a/b'"), ("a\tb", r"'a\tb'"), (5, "5"), (None, "None"),
                           ("s" * 200 + "\n", "'" + "s" * 79)):
            with self.subTest(bad=bad):
                self.assertEqual(base.sid_error(bad),
                                 f'sid must be a non-empty printable string without "/", got {shown}')


class LaunchTest(Refusals):
    def test_defaults(self):
        launch = Launch(("x",))
        self.assertEqual((launch.argv, launch.env, launch.unset, launch.cwd, launch.transcript, launch.resume),
                         (("x",), {}, (), "", "", ""))

    def test_accepts_strings(self):
        launch = Launch(("x", "-y"), env=MappingProxyType({"A": "b"}), unset=("C",), cwd="/c", transcript="/t",
                        resume="x --resume s")
        self.assertEqual((launch.env["A"], launch.unset), ("b", ("C",)))

    def test_refuses_argv_env_or_unset_that_is_not_strings_without_nul(self):
        for field, bads in (("argv", (["x"], ("x\0",), ("x", 1), "x")),
                            ("env", ({"A": 1}, {1: "a"}, {"A\0": "b"}, {"A": "b\0"}, [("A", "b")])),
                            ("unset", (["A"], ("A\0",), ("A", 2)))):
            for bad in bads:
                with self.subTest(field=field, bad=bad):
                    fields = {"argv": ("x",), field: bad}
                    self.refused(lambda: Launch(**fields), f"launch: {field} must be strings without NUL")


class EventsTest(unittest.TestCase):
    def test_the_frozen_set_is_the_seven_events(self):
        self.assertEqual(typing.get_args(base.Event),
                         (SessionBound, Activity, Reply, TurnEnd, Blocked, LimitHit, Exited))
        self.assertNotIn(Stderr, typing.get_args(base.Event))

    def test_defaults(self):
        self.assertEqual((Activity(), TurnEnd(), Blocked(), LimitHit()),
                         (Activity(""), TurnEnd("", 0), Blocked(""), LimitHit(None)))
        self.assertEqual(LimitHit(1767225600.0).resets_at, 1767225600.0)
        for event, name in ((TurnEnd(), "reply"), (LimitHit(), "resets_at")):
            with self.subTest(name=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(event, name, "x")


class ReportsTest(unittest.TestCase):
    def test_usage_and_session_status_are_frozen_records(self):
        usage = Usage(five_hour=120.5, weekly_full=True, resets_at=None)
        self.assertEqual((usage.five_hour, usage.weekly_full, usage.resets_at), (120.5, True, None))
        status = SessionStatus(resumable="yes", last_active=1.5, resume_command="x --resume s")
        self.assertEqual((status.resumable, status.last_active, status.resume_command), ("yes", 1.5, "x --resume s"))
        self.assertEqual(base.RESUMABLE, ("yes", "no", "unknown"))
        for record, name in ((usage, "five_hour"), (status, "resumable")):
            with self.subTest(name=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, name, "x")


class AdapterTest(Refusals):
    def test_the_abstract_classes_cannot_be_built(self):
        with self.assertRaises(TypeError):
            Adapter({})
        with self.assertRaises(TypeError):
            HeadlessRun(StubAdapter({}))
        with self.assertRaises(TypeError):
            TuiRun(StubAdapter({}))

    def test_refuses_the_first_unknown_key_in_sorted_order(self):
        self.refused(lambda: StubAdapter({"zeta": 1, "alpha": 2, "model": "m"}),
                     "unknown key 'alpha' in StubAdapter's config")

    def test_keeps_a_copy_of_its_config(self):
        config = {"model": "m", "flags": ["-x"]}
        adapter = StubAdapter(config)
        self.assertEqual(adapter.config, config)
        self.assertIsNot(adapter.config, config)

    def test_defaults(self):
        self.assertEqual((Adapter.keys, Adapter.runtime_note, TuiRun.dialogs), (frozenset(), "", ()))
        adapter = StubAdapter({})
        self.assertIsNone(adapter.usage_probe())
        for output in ("", "garbage", '{"type": "rate_limit_event"}'):
            with self.subTest(output=output):
                self.assertIsNone(adapter.usage(output))
        self.assertEqual(adapter.session_status("s", "/c"), SessionStatus("unknown", None, ""))

    def test_runs_keep_their_adapter(self):
        adapter = StubAdapter({})
        self.assertIs(adapter.headless_run().adapter, adapter)
        self.assertIs(adapter.tui_run().adapter, adapter)


class HeadlessRunTest(unittest.TestCase):
    def test_events_parses_each_json_object_line_in_order_and_skips_the_rest(self):
        lines = ['{"texts": ["a"]}\n', "not json\n", "[1, 2]\n", "\n", "", '"text"\n', "null\n",
                 "[" * 100000 + "]" * 100000 + "\n", "{}\n", '{"texts": ["b", "c"]}']
        self.assertEqual(list(StubHeadlessRun(StubAdapter({})).events(lines)), [Reply("a"), Reply("b"), Reply("c")])


class TuiRunTest(unittest.TestCase):
    def setUp(self):
        self.run = CountingTuiRun(StubAdapter({}))
        self.stop = {"session_id": "n1", "last": "done", "tasks": [1, 2]}

    def test_submit_pastes_then_presses_enter(self):
        self.assertEqual(self.run.submit("x"), (Paste("x"), Key("Enter")))

    def test_pending_is_0_unless_overridden(self):
        self.assertEqual(StubTuiRun(StubAdapter({})).pending(self.stop), 0)

    def test_stop_with_a_new_session_id_binds_it_first(self):
        self.assertEqual(self.run.hook_events("Stop", self.stop), [SessionBound("n1"), TurnEnd("done", 2)])
        self.assertEqual(self.run.hook_events("Stop", self.stop, bound="n0"), [SessionBound("n1"), TurnEnd("done", 2)])

    def test_stop_with_the_bound_or_no_session_id_is_a_turn_end(self):
        self.assertEqual(self.run.hook_events("Stop", self.stop, bound="n1"), [TurnEnd("done", 2)])
        for session in ({}, {"session_id": ""}, {"session_id": 5}):
            with self.subTest(session=session):
                self.assertEqual(self.run.hook_events("Stop", {"last": "r", **session}), [TurnEnd("r", 0)])

    def test_post_tool_use_is_activity_naming_the_tool(self):
        for payload, detail in (({"tool_name": "Bash"}, "Bash"), ({"tool_name": 5}, ""), ({}, "")):
            with self.subTest(payload=payload):
                self.assertEqual(self.run.hook_events("PostToolUse", {"session_id": "n1", **payload}, bound="n1"),
                                 [Activity(detail)])

    def test_any_other_hook_gives_nothing_more(self):
        self.assertEqual(self.run.hook_events("Notification", {"session_id": "n1"}, bound="n1"), [])
        self.assertEqual(self.run.hook_events("Notification", {"session_id": "n1"}), [SessionBound("n1")])


class NoProcessTest(unittest.TestCase):
    def test_nothing_starts_a_process(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("Popen")) as popen, \
                mock.patch("subprocess.run", side_effect=AssertionError("run")) as run:
            adapter = StubAdapter({"model": "m"})
            tui = adapter.tui_run()
            tui.submit("x")
            tui.hook_events("Stop", {"session_id": "n1"})
            tui.hook_events("PostToolUse", {"tool_name": "Bash"}, bound="n1")
            list(adapter.headless_run().events(['{"texts": ["a"]}']))
            adapter.usage_probe()
            adapter.usage("output")
            adapter.session_status("s", "/c")
        popen.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
