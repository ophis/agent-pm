"""lifecycle test behavior."""

import dataclasses
import inspect
import typing
from unittest import mock

from agent_pm.session.adapters import types as base
from agent_pm.session.adapters.types import (
    ACTIVITY,
    BLOCKED,
    TURN_END,
    Adapter,
    Headless,
    InProcess,
    Tui,
)
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    Exited,
    Key,
    LimitHit,
    Paste,
    Reply,
    SessionBound,
    SessionRequest,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)

from session.tests.case import Case
from session.tests.unit.adapters.base_fixtures import (
    UUID,
    CheckOnlyInProcess,
    DetailedTui,
    LabeledTui,
    NativeIdTui,
    Refusals,
    StubAdapter,
    StubHeadless,
    StubInProcess,
    StubTui,
)


class EventsTest(Case):
    def test_the_frozen_set_is_the_seven_events(self):
        self.assertEqual(
            typing.get_args(engine_types.Event),
            (SessionBound, Activity, Reply, TurnEnd, Blocked, LimitHit, Exited),
        )
        self.assertNotIn(Stderr, typing.get_args(engine_types.Event))

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
        for record, name in ((window, "used_percent"), (usage, "five_hour"), (status, "resumable")):
            with self.subTest(name=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(record, name, "x")


class AdapterTest(Refusals):
    def test_the_abstract_classes_cannot_be_built(self):
        with self.assertRaises(TypeError):
            Adapter({})
        with self.assertRaises(TypeError):
            Headless()
        with self.assertRaises(TypeError):
            Tui()
        with self.assertRaises(TypeError):
            InProcess()

    def test_refuses_the_first_unknown_key_in_sorted_order(self):
        self.refused(
            lambda: StubAdapter({"zeta": 1, "alpha": 2, "model": "m"}),
            "unknown key 'alpha' in StubAdapter's config",
        )

    def test_selects_a_session_through_its_selection_syntax(self):
        adapter = StubAdapter({})
        self.assertEqual(adapter.select_session("n", resume=True), ())
        adapter.selection, adapter.selection_uuid = (("--new",), ("--pick",)), True
        self.assertEqual(adapter.select_session(UUID, resume=False), ("--new", UUID))
        self.assertEqual(adapter.select_session(UUID, resume=True), ("--pick", UUID))
        self.refused(
            lambda: adapter.select_session("n", resume=True), "session ids are UUIDs, got 'n'"
        )

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
        info = engine_types.SessionInfo(
            "s", "n", "/c", project=False, transcript="/t", resume="resume n"
        )
        self.assertIs(StubAdapter({}).session_metadata(info), info)

    def test_writable_dirs_are_none_unless_overridden(self):
        spec = Spec(workdir="/w", access=Access(dirs=("/d",), cwd="/c"), sid="s")
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertEqual(
                    StubAdapter({}).writable_dirs(SessionRequest(spec, resumed=resume)), ()
                )

    def test_in_process_is_none_unless_overridden(self):
        self.assertEqual(Adapter.__abstractmethods__, frozenset({"headless", "tui"}))
        self.assertIsNone(StubAdapter({}).in_process())


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
        self.assertEqual(list(StubHeadless().events(lines)), [Reply("a"), Reply("b"), Reply("c")])


class InProcessTest(Case):
    def test_run_is_its_only_abstract_method(self):
        self.assertEqual(InProcess.__abstractmethods__, frozenset({"run"}))
        with self.assertRaises(TypeError):
            CheckOnlyInProcess()

    def test_checks_nothing_by_default(self):
        run = StubInProcess()
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertIsNone(
                    run.check(SessionRequest(Spec(workdir="/w", sid="s"), resumed=resume))
                )


class TuiTest(Case):
    def setUp(self):
        self.run = DetailedTui()
        self.end = {"session_id": "n1", "last": "done", "tasks": [1, 2]}

    def test_the_hook_kinds(self):
        self.assertEqual((TURN_END, BLOCKED, ACTIVITY), ("turn_end", "blocked", "activity"))

    def test_defaults(self):
        run = StubTui()
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
        run = LabeledTui()
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
        run = StubTui()
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
        run = NativeIdTui()
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
            labeled = DetailedTui()
            for label in labeled.hooks:
                labeled.hook_events(label, {"session_id": "n1", "doing": "Bash"}, bound="n1")
            tui.limit("screen")
            list(adapter.headless().events(['{"texts": ["a"]}']))
            adapter.usage_probe()
            adapter.usage("output")
            adapter.session_status("s", "/c")
            adapter.in_process()
            StubInProcess().check(SessionRequest(Spec(workdir="/w"), resumed=False))
        popen.assert_not_called()
        run.assert_not_called()
