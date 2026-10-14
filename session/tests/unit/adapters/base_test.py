"""Session behavior tests with caller-owned dependencies."""

import dataclasses
import inspect
import operator
import typing
from types import MappingProxyType
from unittest import mock

from agent_pm.session.adapters import base
from agent_pm.session.adapters.base import Adapter, Headless, InProcess, JsonHeadless, Tui
from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.adapters.codex.tui import CodexTui
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    Effort,
    Exited,
    HookKind,
    Key,
    Launch,
    LimitHit,
    Paste,
    Reply,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    Usage,
    Window,
)
from agent_pm.utils import sid_error

from session.tests.case import Case

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class StubAdapter(Adapter):
    keys = frozenset({"model", "flags"})

    def headless(self):
        return StubHeadless()

    def tui(self):
        return StubTui()


class StubHeadless(JsonHeadless):
    def launch(self, spec: Spec):
        return Launch(("stub", spec.prompt))

    def parse(self, record):
        return [Reply(text) for text in record.get("texts", [])]


class StreamHeadless(Headless):
    """Reads its runtime's lines itself: no parse."""

    def launch(self, spec: Spec):
        return Launch(("stub", spec.prompt))

    def events(self, lines):
        for line in lines:
            yield Reply(line.strip())


class StubTui(Tui):
    def launch(self, spec: Spec, hook, *, source_roots: tuple[str, ...]):
        return Launch(("stub", *hook))

    def reply(self, payload):
        return payload.get("last", "")


class StubInProcess(InProcess):
    def run(self, spec: Spec):
        yield Reply(spec.prompt)


class CheckOnlyInProcess(InProcess):
    def check(self, spec: Spec):
        return None


class LabeledTui(StubTui):
    hooks = {"end": HookKind.TURN_END, "wait": HookKind.BLOCKED, "work": HookKind.ACTIVITY}


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


class EventsTest(Case):
    def test_the_frozen_set_is_the_six_events(self):
        self.assertEqual(
            typing.get_args(engine_types.SessionEvent),
            (Activity, Reply, TurnEnd, TurnFailed, Blocked, LimitHit, Exited),
        )
        for other in (Stderr, ClientBound):
            self.assertNotIn(other, typing.get_args(engine_types.SessionEvent))

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
    def test_the_mode_interfaces_cannot_be_built(self):
        for interface in (Headless, JsonHeadless, Tui, InProcess):
            with self.subTest(interface=interface.__name__), self.assertRaises(TypeError):
                interface()

    def test_mode_objects_get_no_adapter_from_their_base(self):
        for interface in (Headless, JsonHeadless, Tui, InProcess):
            with self.subTest(interface=interface.__name__):
                self.assertIs(interface.__init__, object.__init__)

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

    def test_two_adapters_share_no_mutable_config_even_from_a_read_only_one(self):
        for config in (
            {"flags": ["-x"]},
            MappingProxyType({"flags": ("-x", ["-y"]), "model": MappingProxyType({"a": "b"})}),
        ):
            first, second = StubAdapter(config), StubAdapter(config)
            with self.subTest(config=config):
                flags = first.config["flags"]
                (flags if isinstance(flags, list) else flags[1]).append("-z")
                self.assertEqual(second.config, StubAdapter(config).config)
                self.assertNotIn("-z", str(config))
                self.assertIs(type(first.config), dict)

    def test_the_shared_default_maps_are_read_only(self):
        for name, mapping in (
            ("Adapter.child_defaults", Adapter.child_defaults),
            ("Tui.hooks", Tui.hooks),
            ("CodexAdapter.child_defaults", CodexAdapter.child_defaults),
            ("ClaudeAdapter.child_defaults", ClaudeAdapter.child_defaults),
            ("ClaudeTui.hooks", ClaudeTui.hooks),
            ("CodexTui.hooks", CodexTui.hooks),
        ):
            with self.subTest(name=name), self.assertRaises(TypeError):
                operator.setitem(mapping, "X", "1")

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
        self.assertEqual(
            adapter.session_status(
                engine_types.SessionInfo(sid="s", cwd="/c", project=False, transcript="", resume="")
            ),
            SessionStatus("unknown", None, ""),
        )

    def test_session_metadata_preserves_info_by_default(self):
        info = engine_types.SessionInfo("s", "/c", project=False, transcript="/t", resume="r")
        self.assertIs(StubAdapter({}).session_metadata(info), info)

    def test_writable_dirs_are_none_unless_overridden(self):
        for resume in (False, True):
            spec = Spec(workdir="/w", access=Access(dirs=("/d",), cwd="/c"), sid="s", resume=resume)
            with self.subTest(resume=resume):
                self.assertEqual(StubAdapter({}).writable_dirs(spec), ())

    def test_each_mode_factory_is_optional_and_none_unless_overridden(self):
        self.assertFalse(inspect.isabstract(Adapter))
        bare = Adapter({})
        self.assertEqual((bare.headless(), bare.tui(), bare.in_process()), (None, None, None))
        adapter = StubAdapter({})
        self.assertIsInstance(adapter.headless(), StubHeadless)
        self.assertIsInstance(adapter.tui(), StubTui)
        self.assertIsNone(adapter.in_process())

    def test_each_factory_call_is_a_fresh_mode_object(self):
        adapter = StubAdapter({})
        for factory in (adapter.headless, adapter.tui):
            with self.subTest(factory=factory.__name__):
                self.assertIsNot(factory(), factory())


class SignatureTest(Case):
    def test_mode_interfaces_take_the_spec_alone(self):
        for method, params in (
            (Headless.launch, ["self", "spec"]),
            (Headless.events, ["self", "lines"]),
            (JsonHeadless.parse, ["self", "record"]),
            (Tui.launch, ["self", "spec", "hook", "source_roots"]),
            (InProcess.run, ["self", "spec"]),
            (InProcess.check, ["self", "spec"]),
            (Adapter.writable_dirs, ["self", "spec"]),
        ):
            with self.subTest(method=method.__qualname__):
                self.assertEqual(list(inspect.signature(method).parameters), params)


class HeadlessTest(Case):
    def test_the_stream_is_required_and_parse_is_not(self):
        self.assertEqual(Headless.__abstractmethods__, frozenset({"launch", "events"}))
        self.assertFalse(hasattr(Headless, "parse"))
        self.assertEqual(list(StreamHeadless().events(["a\n", "b"])), [Reply("a"), Reply("b")])

    def test_json_headless_supplies_the_stream_from_parse(self):
        self.assertTrue(issubclass(JsonHeadless, Headless))
        self.assertEqual(JsonHeadless.__abstractmethods__, frozenset({"launch", "parse"}))

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
                self.assertIsNone(run.check(Spec(workdir="/w", sid="s", resume=resume)))


class TuiTest(Case):
    def setUp(self):
        self.run = DetailedTui()
        self.end = {"session_id": "n1", "last": "done", "tasks": [1, 2]}

    def test_the_hook_kinds_live_only_in_hook_kind(self):
        for name in ("TURN_END", "BLOCKED", "ACTIVITY"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(base, name))

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

    def test_a_new_session_id_binds_it_first_once_per_tui(self):
        self.assertEqual(
            self.run.hook_events("end", self.end), [ClientBound("n1"), TurnEnd("done", 2)]
        )
        self.assertEqual(self.run.hook_events("end", self.end), [TurnEnd("done", 2)])
        self.assertEqual(
            self.run.hook_events("end", {**self.end, "session_id": "n2"}),
            [ClientBound("n2"), TurnEnd("done", 2)],
        )
        self.assertEqual(
            DetailedTui().hook_events("end", self.end),
            [ClientBound("n1"), TurnEnd("done", 2)],
        )

    def test_no_session_id_binds_nothing(self):
        for session in ({}, {"session_id": ""}, {"session_id": 5}):
            with self.subTest(session=session):
                self.assertEqual(
                    self.run.hook_events("end", {"last": "r", **session}), [TurnEnd("r", 0)]
                )

    def test_each_label_gives_the_event_of_its_kind(self):
        payload = {"session_id": "n1", "last": "r", "tasks": [1], "doing": "Bash", "why": "ask"}
        self.run.hook_events("other", payload)
        for label, event in (
            ("end", TurnEnd("r", 1)),
            ("wait", Blocked("ask")),
            ("work", Activity("Bash")),
        ):
            with self.subTest(label=label):
                self.assertEqual(self.run.hook_events(label, payload), [event])

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
        self.assertEqual(self.run.hook_events("other", {"session_id": "n1"}), [ClientBound("n1")])
        for label in ("other", "", "End", HookKind.TURN_END, "Stop", "PostToolUse"):
            with self.subTest(label=label):
                self.assertEqual(self.run.hook_events(label, {"session_id": "n1", "last": "r"}), [])

    def test_without_hooks_no_label_has_a_kind(self):
        run = StubTui()
        payload = {"last": "r", "tool_name": "Bash"}
        for label in ("Stop", "PostToolUse", "Notification", "PermissionRequest", "end"):
            with self.subTest(label=label):
                self.assertEqual(run.hook_events(label, payload), [])

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
            [ClientBound("t1"), TurnEnd("r", 0)],
        )
        self.assertEqual(run.hook_events("end", {"thread": "t1", "last": "r"}), [TurnEnd("r", 0)])
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
                labeled.hook_events(label, {"session_id": "n1", "doing": "Bash"})
            tui.limit("screen")
            list(adapter.headless().events(['{"texts": ["a"]}']))
            adapter.usage_probe()
            adapter.usage("output")
            adapter.session_status(
                engine_types.SessionInfo(sid="s", cwd="/c", project=False, transcript="", resume="")
            )
            adapter.in_process()
            StubInProcess().check(Spec(workdir="/w"))
        popen.assert_not_called()
        run.assert_not_called()


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
            Access(("/a",))

    def test_refuses_dirs_commands_or_reads_that_are_not_a_tuple_of_strings(self):
        for bad in (["/a"], ("/a", 1), "/a", None):
            with self.subTest(bad=bad):
                self.refused(lambda *, bad=bad: Access(dirs=bad), "dirs must be a tuple of strings")
                self.refused(
                    lambda *, bad=bad: Access(commands=bad), "commands must be a tuple of strings"
                )
                self.refused(
                    lambda *, bad=bad: Access(reads=bad), "reads must be a tuple of strings"
                )

    def test_accepts_an_env_of_exec_names_to_strings(self):
        env = {
            "PYTHONPATH": "/a b;c",
            "_x1": "",
            "A-B": "v",
            "1A": "v",
            "X;Y": "v",
            "A B": "v",
            "A\n": "v",
            "Ä": "v",
        }
        self.assertEqual(Access(env=MappingProxyType(env)).env, env)

    def test_refuses_invalid_exec_env_names_and_values(self):
        message = "access env must map variable names to strings without NUL"
        for bad in (
            {"A=B": "a"},
            {"A\0": "a"},
            {"": "a"},
            {1: "a"},
            {"A": 1},
            {"A": None},
            {"A": "b\0"},
            [("A", "b")],
            None,
        ):
            with self.subTest(bad=bad):
                self.refused(lambda *, bad=bad: Access(env=bad), message)


class SpecTest(Refusals):
    def test_defaults(self):
        spec = Spec(workdir="/w")
        self.assertEqual(
            (spec.access, spec.tier, spec.effort, spec.sid), (Access(), None, None, None)
        )
        self.assertEqual(
            (spec.flags, spec.env, spec.name, spec.protect, spec.reply_tags),
            ((), {}, None, (), None),
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
                    lambda *, bad=bad: Spec(workdir="/w", flags=bad),
                    "flags must be a tuple of strings without NUL",
                )

    def test_refuses_env_that_does_not_map_variable_names_to_strings_without_nul(self):
        for bad in (
            {"A": 1},
            {"A": None},
            {"A": "b\0"},
            {1: "a"},
            {"": "a"},
            {"A=": "a"},
            {"A\0": "a"},
            [("A", "b")],
            "A=b",
            None,
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", env=bad),
                    "env must map variable names to strings without NUL",
                )

    def test_refuses_a_name_other_than_none_or_a_string(self):
        for bad in (5, b"w", ("w",)):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", name=bad), "name must be None or a string"
                )

    def test_refuses_protect_that_is_not_a_tuple_of_printable_absolute_paths(self):
        for bad in (["/p"], ("p",), ("",), ("/p\n",), ("/p\0",), ("/p", 1), (b"/p",), "/p", None):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", protect=bad),
                    "protect must be a tuple of printable absolute paths",
                )

    def test_reply_tags_are_none_or_a_progress_and_an_outcome_tag(self):
        self.assertEqual(Spec(workdir="/w", reply_tags=("p-1", "o_2")).reply_tags, ("p-1", "o_2"))
        for bad in (
            ["p", "o"],
            ("p",),
            ("p", "o", "x"),
            ("p", ""),
            ("p", "o b"),
            ("p:", "o"),
            ("p", 1),
            "po",
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", reply_tags=bad),
                    "reply_tags must be None or (progress tag, outcome tag), each [A-Za-z0-9_-]+",
                )

    def test_keyword_only_and_frozen(self):
        with self.assertRaises(TypeError):
            Spec("/w")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Spec(workdir="/w").sid = "s"

    def test_accepts_every_tier_and_effort(self):
        for tier in (1, 2, 3, 4):
            for effort in (*Effort, "max"):
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
                    lambda *, bad=bad: Spec(workdir=bad),
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
                    lambda *, bad=bad: Spec(workdir="/w", tier=bad),
                    f"tier must be an integer 1–4, got {shown}",
                )

    def test_refuses_an_effort_outside_efforts(self):
        for bad, shown in (
            ("extreme", "'extreme'"),
            ("", "''"),
            ("LOW", "'LOW'"),
            (3, "3"),
            (["low"], "['low']"),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", effort=bad),
                    f"effort must be one of low, medium, high, xhigh, max, got {shown}",
                )

    def test_refuses_an_access_cwd_that_is_not_a_printable_absolute_path(self):
        for bad, shown in (("rel", "'rel'"), ("/c\0", r"'/c\x00'")):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(workdir="/w", access=Access(cwd=bad)),
                    f"cwd must be a printable absolute path, got {shown}",
                )

    def test_refuses_access_dirs_that_are_not_printable_absolute_paths(self):
        for bad in ("../../other/agent-pm", "", "/d\n", "/d\0"):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: Spec(
                        workdir="/w",
                        access=Access(cwd="/x/deploy/work", dirs=(bad,)),
                        protect=("/x/other/agent-pm",),
                        runtime="claude",
                    ),
                    "dirs must contain only printable absolute paths",
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
                    lambda *, bad=bad: Spec(workdir="/w", sid=bad),
                    f'sid must be a non-empty printable string without "/", got {shown}',
                )


class SidErrorTest(Case):
    def test_none_for_a_non_empty_printable_string_without_a_slash(self):
        for sid in ("s", "s-1", "0b6c8f9e-2a1d-4c3e-9f7a-5b4d3c2e1f00", "a b", "ü"):
            with self.subTest(sid=sid):
                self.assertIsNone(sid_error(sid))

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
                    sid_error(bad),
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
                        lambda *, fields=fields: Launch(**fields),
                        f"launch: {field} must be strings without NUL",
                    )
