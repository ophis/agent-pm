"""Driver sessions behavior with caller-owned dependencies."""

import dataclasses
import os
from unittest import mock

from agent_pm.session.engine import api
from agent_pm.session.engine.types import (
    ConfigError,
    DriverError,
    SessionRow,
)
from agent_pm.session.hosts.tmux import tmux_host

from session.tests.unit.driver.support import (
    NO_PANE,
    OPENERS,
    PLACEMENTS,
    SID,
    TuiBase,
    listed,
)


class SessionsTest(TuiBase):
    def test_a_row_per_listed_session_in_its_order(self):
        self.host.listing.return_value = [
            listed("mgr"),
            listed("w1", sid=SID, runtime="fake", state="done", pane="%3", opener="mgr", shown="m"),
            listed("w2", sid="a/b", runtime="Fake", state="blocked", shown=""),
            listed("w3", runtime="x-y", state="dead"),
            listed("w4", runtime="fake", state="done", dead=True, last_lines="bye"),
            listed("w5", runtime="fake9_", state="other", dead=None),
        ]
        self.host.own_session.return_value = "mgr"
        row = SessionRow
        self.assertEqual(
            self.driver.sessions(),
            [
                row("mgr", None, None, "working", None, None, None, None, True),
                row("w1", SID, "fake", "done", "%3", "mgr", "m", None, False),
                row("w2", None, None, "blocked", None, None, "", None, False),
                row("w3", None, None, "working", None, None, None, None, False),
                row("w4", None, "fake", "dead", None, None, None, "bye", False),
                row("w5", None, "fake9_", "working", None, None, None, None, False),
            ],
        )
        self.assertEqual(self.host.mock_calls, [mock.call.listing(None), mock.call.own_session()])

    def test_match_is_compiled_for_the_listing_to_fullmatch(self):
        self.assertEqual(self.driver.sessions(r"w[0-9]+"), [])
        ((pattern,),) = [c.args for c in self.host.listing.call_args_list]
        self.assertEqual(pattern.pattern, r"w[0-9]+")
        self.assertEqual(
            [n for n in ("w1", "w12", "xw1", "w1x") if pattern.fullmatch(n)], ["w1", "w12"]
        )

    def test_no_server_is_no_rows_without_reading_tmux_pane(self):
        self.assertEqual(self.driver.sessions(), [])
        self.host.own_session.assert_not_called()

    def test_a_non_str_or_invalid_match_is_refused(self):
        for bad, message in (
            (5, "match must be a regular expression, got 5"),
            (b"w", "match must be a regular expression, got b'w'"),
            ("(", "match '(': missing ), unterminated subpattern at position 0"),
            ("[", "match '[': unterminated character set at position 0"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.driver.sessions(bad)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()

    def test_tmux_failing_or_a_bad_tmux_pane_is_a_driver_error_never_no_rows(self):
        self.host.listing.return_value = None
        with self.assertRaises(DriverError) as caught:
            self.driver.sessions()
        self.assertEqual(str(caught.exception), "tmux sessions unknown: tmux failed")
        self.host.listing.return_value = [listed("w1")]
        self.host.own_session.side_effect = tmux_host.TuiError("bad $TMUX_PANE 'x'")
        with self.assertRaises(DriverError) as caught:
            self.driver.sessions()
        self.assertEqual(str(caught.exception), "tmux sessions unknown: bad $TMUX_PANE 'x'")

    def test_a_row_is_frozen_with_these_fields(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(SessionRow)],
            ["name", "sid", "runtime", "state", "pane", "opener", "shown", "last_lines", "own"],
        )
        found = SessionRow("w1", None, None, "working", None, None, None, None, False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.state = "done"


class CloseTest(TuiBase):
    def test_kills_the_session_and_removes_its_dir(self):
        self.tui(name="w1")
        self.host.reset_mock()
        self.driver.close("w1")
        self.assertEqual(self.host.mock_calls, [mock.call.kill("w1")])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))
        self.assertTrue(os.path.isdir(self.root))

    def test_a_bad_name_is_refused_before_tmux_or_a_path(self):
        self.name_refused(self.driver.close)


class ShowTest(TuiBase):
    def setUp(self):
        super().setUp()
        self.host.status.return_value = tmux_host.RUNNING
        self.host.open_pane.return_value = None

    def test_none_once_a_pane_shows_it_placed_with_the_tui_settings(self):
        self.configure(tui=True, settings={"workers_per_column": 2, "grid_retile": "off"})
        self.assertIsNone(
            self.driver.show("w1", split="below", split_from="m1", opener="w0t0p0:AB-12")
        )
        self.assertEqual(
            self.host.mock_calls,
            [
                mock.call.status("w1"),
                mock.call.open_pane(
                    "w1",
                    options=tmux_host.PaneOptions(
                        split="below",
                        split_from="m1",
                        opener="w0t0p0:AB-12",
                        per_column=2,
                        retile="off",
                    ),
                ),
            ],
        )

    def test_no_pane_is_the_attach_command_the_reason_logged(self):
        self.host.status.return_value = 0
        self.host.open_pane.return_value = f"{NO_PANE}; watch it with tmux attach -t '=w1'"
        with (
            mock.patch.dict(os.environ, {"TUI_ATTACH_PREFIX": "docker exec -it box"}),
            self.assertLogs("driver") as logs,
        ):
            self.assertEqual(self.driver.show("w1"), "docker exec -it box tmux attach -t '=w1'")
        self.assertEqual(
            logs.output, [f"WARNING:driver:show w1: {NO_PANE}; watch it with tmux attach -t '=w1'"]
        )
        self.host.open_pane.assert_called_once_with(
            "w1",
            options=tmux_host.PaneOptions(
                split=None, split_from=None, opener=None, per_column=None, retile=None
            ),
        )

    def test_an_absent_session_is_a_driver_error(self):
        self.host.status.return_value = None
        with self.assertRaises(DriverError) as caught:
            self.driver.show("w1")
        self.assertEqual(str(caught.exception), "no tmux session w1")
        self.host.open_pane.assert_not_called()

    def test_a_bad_name_placement_opener_or_setting_is_refused_before_tmux(self):
        self.name_refused(self.driver.show)
        for kwargs, message in (*PLACEMENTS, *(({"opener": o}, m) for o, m in OPENERS)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError) as caught:
                self.driver.show("w1", **kwargs)
            self.assertEqual(str(caught.exception), message)
        self.configure(tui=True, settings={"grid_retile": "never"})
        with self.assertRaises(ConfigError) as caught:
            self.driver.show("w1")
        self.assertEqual(str(caught.exception), "grid_retile: want one of all, open-close, off")
        self.nothing_hosted()


class LayoutTest(TuiBase):
    def test_the_callers_opener_once_its_pane_is_found(self):
        self.host.opener.return_value = "mgr"
        self.assertEqual(self.driver.layout(), "mgr")
        self.assertEqual(self.host.mock_calls, [mock.call.anchor(None), mock.call.opener()])

    def test_beside_a_live_split_from_the_opener_may_be_none(self):
        self.host.status.return_value = tmux_host.RUNNING
        self.host.opener.side_effect = tmux_host.TuiError(NO_PANE)
        self.assertIsNone(self.driver.layout("below", "m1"))
        self.assertEqual(
            self.host.mock_calls,
            [mock.call.status("m1"), mock.call.anchor("m1"), mock.call.opener()],
        )

    def test_no_pane_or_no_opener_is_a_driver_error_with_a_hint_without_split_from(self):
        hint = ": run from tmux or iTerm2, or pass split_from"
        self.host.status.return_value = tmux_host.RUNNING
        for split_from, anchor, opener, message in (
            (None, NO_PANE, None, f"({NO_PANE}){hint}"),
            (None, None, "bad $ITERM_SESSION_ID 'x'", f"(bad $ITERM_SESSION_ID 'x'){hint}"),
            (
                "m1",
                "no terminal shows tmux session m1",
                None,
                "(no terminal shows tmux session m1)",
            ),
        ):
            with self.subTest(split_from=split_from, anchor=anchor):
                self.host.anchor.side_effect = anchor and tmux_host.TuiError(anchor)
                self.host.opener.side_effect = opener and tmux_host.TuiError(opener)
                with self.assertRaises(DriverError) as caught:
                    self.driver.layout(split_from=split_from)
                self.assertEqual(str(caught.exception), f"no pane to show the TUI beside {message}")

    def test_a_bad_split_or_split_from_or_an_absent_one_is_refused(self):
        for kwargs, message in PLACEMENTS:
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError) as caught:
                self.driver.layout(**kwargs)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()
        with self.assertRaises(ConfigError) as caught:
            self.driver.layout(split_from="m1")
        self.assertEqual(str(caught.exception), "no tmux session m1")
        self.assertEqual(self.host.mock_calls, [mock.call.status("m1")])


class AttachCommandTest(TuiBase):
    def test_tmux_hosts_command_after_the_name_check(self):
        self.assertEqual(self.driver.attach_command("w1"), "tmux attach -t '=w1'")
        with mock.patch.dict(os.environ, {"TUI_ATTACH_PREFIX": "docker exec -it box"}):
            self.assertEqual(
                self.driver.attach_command("w1"), "docker exec -it box tmux attach -t '=w1'"
            )
        self.host.reset_mock()
        self.name_refused(self.driver.attach_command)


class DetachTest(TuiBase):
    def test_hosts_argv_with_the_signals_blocked_and_the_opener(self):
        env = {"PATH": "/bin", "TMUX": "x"}
        self.assertIsNone(self.driver.detach("d1", ["tool", "a b"], self.dir, env, opener="mgr"))
        self.assertEqual(
            self.host.mock_calls,
            [
                mock.call.detach(
                    "d1",
                    tmux_host.Command(argv=["tool", "a b"], cwd=self.dir, env=env),
                    options=tmux_host.DetachOptions(block=api.SIGNALS, opener="mgr"),
                )
            ],
        )
        self.driver.detach("d2", ("tool",), self.dir, env)
        self.assertEqual(self.host.detach.call_args.kwargs["options"].opener, None)

    def test_a_bad_name_argv_cwd_or_opener_is_refused_before_tmux(self):
        self.name_refused(lambda name: self.driver.detach(name, ["tool"], self.dir, {}))
        missing, file = os.path.join(self.dir, "missing"), self.scenario
        for args, kwargs, message in (
            (([], self.dir), {}, "argv must be a non-empty list of str, got []"),
            (("tool", self.dir), {}, "argv must be a non-empty list of str, got 'tool'"),
            (([b"tool"], self.dir), {}, "argv must be a non-empty list of str, got [b'tool']"),
            ((["tool"], "work"), {}, "cwd must be a printable absolute path, got 'work'"),
            ((["tool"], "/a\nb"), {}, r"cwd must be a printable absolute path, got '/a\nb'"),
            *(
                ((["tool"], cwd), {}, f"cwd must be an existing directory, got {cwd!r:.80}")
                for cwd in (missing, file)
            ),
            *(((["tool"], self.dir), {"opener": o}, m) for o, m in OPENERS),
        ):
            with (
                self.subTest(args=args, kwargs=kwargs),
                self.assertRaises(ConfigError) as caught,
            ):
                self.driver.detach("d1", *args, {}, **kwargs)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()

    def test_a_tmux_failure_is_a_driver_error(self):
        self.host.detach.side_effect = tmux_host.TuiError("session d1 exists")
        with self.assertRaises(DriverError) as caught:
            self.driver.detach("d1", ["tool"], self.dir, {})
        self.assertEqual(str(caught.exception), "detach d1: session d1 exists")
