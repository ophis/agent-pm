import dataclasses
import inspect
import operator
import signal
import typing
from types import MappingProxyType

from agent_pm import session
from agent_pm.session import (
    Access,
    ActivityState,
    ConfigError,
    DriverError,
    Effort,
    Hosts,
    Liveness,
    Mode,
    NotDisplayable,
    Resumable,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
)
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.types import (
    ClientBound,
    HookError,
    HookKind,
    Launch,
    Prepared,
    ProcessOptions,
    Tier,
    Word,
)

from session.tests.case import Case


class EnvironmentSources(Case):
    def test_spec_accepts_both_environment_sources(self):
        spec = Spec(workdir="/w", access=Access(env={"A": "1"}), env={"B": "2"})
        self.assertEqual((spec.access.env, dict(spec.env)), ({"A": "1"}, {"B": "2"}))

    def test_every_input_accepts_exec_environment_names(self):
        env = {"A-B": "a b", "1A": "", "X;Y": "value", "Ä": "value"}
        for build in (
            Access,
            ProcessOptions,
            lambda **fields: Spec(workdir="/w", **fields),
            lambda **fields: Launch(("x",), **fields),
        ):
            with self.subTest(build=build):
                self.assertEqual(dict(build(env=env).env), env)

    def test_every_input_rejects_invalid_exec_names_and_values(self):
        for env in (
            {"": "secret"},
            {"A=B": "secret"},
            {"A\0": "secret"},
            {"A": "secret\0"},
            {1: "secret"},
            {"A": 1},
            None,
        ):
            for build in (
                Access,
                ProcessOptions,
                lambda **fields: Spec(workdir="/w", **fields),
                lambda **fields: Launch(("x",), **fields),
            ):
                with self.subTest(env=env, build=build), self.assertRaises(ConfigError) as caught:
                    build(env=env)
                self.assertNotIn("secret", str(caught.exception))


class ProcessOptionsTest(Case):
    def test_options_own_an_immutable_copy_of_the_environment(self):
        source = {"TOKEN": "original"}
        options = ProcessOptions(flags=("--flag",), env=source)
        source["TOKEN"] = "changed"
        self.assertEqual((options.flags, dict(options.env)), (("--flag",), {"TOKEN": "original"}))
        with self.assertRaises(TypeError):
            options.env["TOKEN"] = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            options.flags = ()


INFO = SessionInfo(sid="s", cwd="/w", project=False, transcript="", resume="")


class FrozenLaunchData(Case):
    def test_signal_mask_is_explicit_and_immutable(self):
        self.assertEqual(Launch(("x",)).blocked_signals, ())
        signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        launch = Launch(("x",), blocked_signals=signals)
        self.assertEqual(launch.blocked_signals, signals)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            launch.blocked_signals = ()
        for invalid in (("TERM",), (True,), (0,), (-1,), [signal.SIGTERM]):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                Launch(("x",), blocked_signals=invalid)

    def test_access_launch_and_prepared_keep_a_copy_of_the_callers_env(self):
        for build in (
            lambda env: Access(env=env),
            lambda env: Launch(("x",), env=env),
            lambda env: Prepared(Spec(workdir="/w"), INFO, ("x",), env),
        ):
            source = {"A": "1"}
            held = build(source)
            source["B"] = "2"
            del source["A"]
            self.assertEqual(dict(held.env), {"A": "1"})

    def test_access_launch_and_prepared_env_are_read_only(self):
        for held in (
            Access(),
            Access(env={"A": "1"}),
            Launch(("x",)),
            Launch(("x",), env={"A": "1"}),
            Prepared(Spec(workdir="/w"), INFO),
            Prepared(Spec(workdir="/w"), INFO, ("x",), {"A": "1"}),
        ):
            with self.subTest(held=held), self.assertRaises(TypeError):
                held.env["C"] = "3"

    def test_default_specs_do_not_share_one_access(self):
        first, second = Spec(workdir="/w"), Spec(workdir="/w")
        self.assertIsNot(first.access, second.access)
        self.assertEqual(first.access, Access())

    def test_frozen_copies_compare_by_value(self):
        self.assertEqual(Access(env={"A": "1"}), Access(env=MappingProxyType({"A": "1"})))
        self.assertNotEqual(Access(env={"A": "1"}), Access(env={"A": "2"}))
        self.assertEqual(Launch(("x",), env={"A": "1"}), Launch(("x",), env={"A": "1"}))
        self.assertNotEqual(Launch(("x",), env={"A": "1"}), Launch(("x",), env={"A": "2"}))
        self.assertEqual(Spec(workdir="/w"), Spec(workdir="/w", access=Access()))
        self.assertEqual(
            Spec(workdir="/w", access=Access(env={"A": "1"})),
            Spec(workdir="/w", access=Access(env={"A": "1"})),
        )


class FrozenConfig(Case):
    def test_hosts_hold_a_read_only_deep_copy_of_their_tables(self):
        source = {"x": {"implementation": "p.m:X", "sizes": [1]}}
        hosts = Hosts("x", source)
        source["x"]["sizes"].append(2)
        source["y"] = {}
        self.assertEqual(hosts.tables, {"x": {"implementation": "p.m:X", "sizes": (1,)}})
        for table, key in ((hosts.tables, "y"), (hosts.tables["x"], "sizes")):
            with self.subTest(key=key), self.assertRaises(TypeError):
                operator.setitem(table, key, ())
        self.assertEqual(Hosts("x", hosts.tables), hosts)


class WhatToStart(Case):
    def test_the_spec_constructor_stays_keyword_only(self):
        parameters = inspect.signature(Spec).parameters
        self.assertTrue(parameters)
        self.assertTrue(all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values()))
        with self.assertRaises(TypeError):
            Spec("/w")

    def test_the_start_fields_default_so_an_adapters_partial_spec_stays_valid(self):
        spec = Spec(workdir="/w")
        self.assertEqual(
            (spec.runtime, spec.mode, spec.prompt, spec.resume, spec.events),
            (None, None, "", False, None),
        )

    def test_idle_timeout_is_no_spec_field_but_the_callers_watch_option(self):
        with self.assertRaises(TypeError):
            Spec(workdir="/w", idle_timeout=2.5)

    def test_a_full_spec_keeps_its_fields_its_mode_a_mode(self):
        spec = Spec(
            workdir="/w",
            sid="s1",
            runtime="fake",
            mode="tui",
            prompt="hi",
            resume=True,
            events="/w/events",
        )
        self.assertIs(spec.mode, Mode.TUI)
        self.assertEqual(
            (spec.runtime, spec.prompt, spec.resume, spec.events),
            ("fake", "hi", True, "/w/events"),
        )

    def test_bad_start_fields_are_config_errors(self):
        modes = ", ".join(Mode)
        for fields, message in (
            ({"resume": True}, "resume: needs spec.sid, the session to resume"),
            ({"sid": "s1", "resume": "yes"}, "resume must be a bool, got 'yes'"),
            ({"mode": "pane"}, f"mode must be one of {modes}, got 'pane'"),
            ({"mode": ""}, f"mode must be one of {modes}, got ''"),
            ({"mode": 5}, f"mode must be one of {modes}, got 5"),
            ({"runtime": 5}, "runtime must be None or a string"),
            ({"prompt": None}, "prompt must be a string"),
            ({"events": 5}, "events must be a printable path, got 5"),
            ({"events": "/a\nb"}, r"events must be a printable path, got '/a\nb'"),
        ):
            with self.subTest(fields=fields), self.assertRaises(ConfigError) as caught:
                Spec(workdir="/w", **fields)
            self.assertEqual(str(caught.exception), message)


class DisplayError(Case):
    def test_not_displayable_is_a_driver_error(self):
        self.assertTrue(issubclass(NotDisplayable, DriverError))
        self.assertEqual(
            str(NotDisplayable("host fake cannot display")), "host fake cannot display"
        )


class OptionSets(Case):
    def test_each_option_set_is_a_str_enum_of_its_values(self):
        for enum, values in (
            (Mode, ["headless", "tui", "in_process"]),
            (Liveness, ["live", "absent", "unknown"]),
            (Effort, ["low", "medium", "high", "xhigh", "max"]),
            (Resumable, ["yes", "no", "unknown"]),
            (ActivityState, ["working", "done", "blocked", "dead"]),
        ):
            with self.subTest(enum=enum.__name__):
                self.assertEqual([m.value for m in enum], values)
                self.assertEqual(list(enum), values)
                self.assertIn(enum.__name__, session.__all__)
        self.assertNotIn("MODES", session.__all__)

    def test_hook_kinds_tiers_and_hook_words_are_session_internal_str_enums(self):
        for enum, values in (
            (HookKind, ["turn_end", "turn_failed", "blocked", "activity"]),
            (Tier, ["1", "2", "3", "4"]),
            (Word, ["done", "blocked", "working"]),
        ):
            with self.subTest(enum=enum.__name__):
                self.assertEqual([m.value for m in enum], values)
                self.assertEqual(list(enum), values)
                self.assertNotIn(enum.__name__, session.__all__)

    def test_a_hook_word_is_an_activity_state(self):
        self.assertLessEqual(set(Word), set(ActivityState))
        self.assertIsNot(Word, ActivityState)
        for word in Word:
            with self.subTest(word=word):
                state = ActivityState(word)
                self.assertEqual(state.value, word.value)
                self.assertIsNot(state, word)
                self.assertIsInstance(state, ActivityState)
        self.assertNotIn(ActivityState.DEAD, Word)


class CarriedEnums(Case):
    """Text is normalised once, where the value is built; the enum is what is carried."""

    def test_a_spec_carries_its_mode_and_effort_as_members(self):
        spec = Spec(workdir="/w", mode="headless", effort="high")
        self.assertIs(spec.mode, Mode.HEADLESS)
        self.assertIs(spec.effort, Effort.HIGH)
        self.assertIs(Spec(workdir="/w", effort=Effort.LOW).effort, Effort.LOW)
        self.assertIsNone(Spec(workdir="/w").effort)

    def test_a_status_carries_a_resumable_given_it_or_its_plain_value(self):
        for resumable in (Resumable.YES, "yes"):
            with self.subTest(resumable=resumable):
                status = SessionStatus(resumable, None, "")
                self.assertIs(status.resumable, Resumable.YES)

    def test_a_row_carries_an_activity_state_given_it_or_its_plain_value(self):
        for state in (ActivityState.DEAD, "dead"):
            with self.subTest(state=state):
                row = SessionRow("w1", None, None, state, None, None, None, None, own=False)
                self.assertIs(row.state, ActivityState.DEAD)

    def test_an_unknown_value_names_the_allowed_ones(self):
        for build, message in (
            (
                lambda: SessionStatus("maybe", None, ""),
                "resumable must be one of yes, no, unknown, got 'maybe'",
            ),
            (
                lambda: SessionStatus(None, None, ""),
                "resumable must be one of yes, no, unknown, got None",
            ),
            (
                lambda: SessionRow("w1", None, None, "idle", None, None, None, None, own=False),
                "state must be one of working, done, blocked, dead, got 'idle'",
            ),
        ):
            with self.subTest(message=message), self.assertRaises(ConfigError) as caught:
                build()
            self.assertEqual(str(caught.exception), message)

    def test_a_tier_is_still_an_integer_one_of_the_tiers(self):
        self.assertEqual([Spec(workdir="/w", tier=int(t)).tier for t in Tier], [1, 2, 3, 4])


class SharedTypes(Case):
    def test_the_hook_and_display_types_live_in_engine_types(self):
        for kind in (HookError, Word, NotDisplayable, Tier, ActivityState):
            with self.subTest(kind=kind.__name__):
                self.assertEqual(kind.__module__, engine_types.__name__)
                self.assertIn(kind.__name__, engine_types.__all__)
        self.assertEqual(HookError.__bases__, (Exception,))


SID = "4f1c2b9e-0d6a-4c1e-9b7a-2e5f8a3d6c10"

FIELDS = {"sid": SID, "cwd": "/a", "project": True, "transcript": "/t/x.jsonl", "resume": "r"}


def bad_field(fields) -> str | None:
    try:
        SessionInfo.from_fields(fields, strict=True)
    except ValueError as e:
        return str(e)
    return None


class Identity(Case):
    def test_persisted_is_its_five_fields_and_no_native_id(self):
        info = SessionInfo(**FIELDS)
        self.assertEqual(info.persisted(), FIELDS)
        self.assertEqual([f.name for f in dataclasses.fields(SessionInfo)], list(FIELDS))
        self.assertNotIn("native_id", inspect.signature(SessionInfo.from_fields).parameters)

    def test_from_fields_round_trips_and_ignores_other_keys(self):
        info = SessionInfo(**FIELDS)
        self.assertEqual(SessionInfo.from_fields(info.persisted()), info)
        extra = {"ts": "t", "kind": "session", "runtime": "fake", "native_id": "n1", **FIELDS}
        self.assertEqual(SessionInfo.from_fields(extra), info)

    def test_missing_transcript_or_resume_is_empty(self):
        fields = {"sid": SID, "cwd": "/a", "project": False}
        self.assertEqual(
            SessionInfo.from_fields(fields),
            SessionInfo(sid=SID, cwd="/a", project=False, transcript="", resume=""),
        )

    def test_invalid_fields_are_none_never_an_error(self):
        for key, bad in (
            ("sid", ""),
            ("sid", "a/b"),
            ("sid", "a\nb"),
            ("sid", 3),
            ("sid", None),
            ("cwd", ""),
            ("cwd", "rel"),
            ("cwd", "/x\x1b[2J"),
            ("cwd", 3),
            ("cwd", None),
            ("project", 1),
            ("project", "yes"),
            ("project", None),
            ("transcript", "rel.jsonl"),
            ("transcript", "/t\n"),
            ("transcript", 3),
            ("resume", 3),
            ("resume", None),
        ):
            with self.subTest(key=key, bad=bad):
                self.assertIsNone(SessionInfo.from_fields({**FIELDS, key: bad}))
                self.assertEqual(bad_field({**FIELDS, key: bad}), key)
        for key in ("sid", "cwd", "project"):
            with self.subTest(missing=key):
                fields = {k: v for k, v in FIELDS.items() if k != key}
                self.assertIsNone(SessionInfo.from_fields(fields))
                self.assertEqual(bad_field(fields), key)
        for bad in (None, [], "x"):
            with self.subTest(fields=bad):
                self.assertIsNone(SessionInfo.from_fields(bad))

    def test_strict_names_the_first_bad_key_in_sid_cwd_transcript_project_resume_order(self):
        self.assertEqual(SessionInfo.from_fields(FIELDS, strict=True), SessionInfo(**FIELDS))
        bad = {"sid": "", "cwd": "", "transcript": "rel", "project": None, "resume": None}
        for key in bad:
            with self.subTest(first=key):
                self.assertEqual(bad_field({**FIELDS, **bad}), key)
                bad = {k: v for k, v in bad.items() if k != key}
        self.assertEqual(bad_field(None), "sid")

    def test_exported(self):
        self.assertIn("SessionInfo", session.__all__)


class Binding(Case):
    def test_a_client_binding_is_an_adapter_notice_never_a_session_event(self):
        self.assertEqual(ClientBound("n1").client_id, "n1")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ClientBound("n1").client_id = "n2"
        self.assertNotIn(ClientBound, typing.get_args(engine_types.SessionEvent))
        self.assertFalse(hasattr(engine_types, "SessionBound"))
        self.assertNotIn("ClientBound", session.__all__)
