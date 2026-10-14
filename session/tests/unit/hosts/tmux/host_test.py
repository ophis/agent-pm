"""TmuxHost's display format, detach serialization and refusals, and Displayable forwarding over
recorded tmux calls. Attachment and TmuxProcess cases use real controller metadata in
session/tests/integration/hosts/tmux/process_integration_test.py.
"""

import json
import logging
import os
import re
import signal
from pathlib import Path
from unittest import mock

from agent_pm.session import ConfigError, Displayable, DriverError, Host, Launch, Terminal
from agent_pm.session.engine.types import Paste
from agent_pm.session.hosts.base import SIGNALS
from agent_pm.session.hosts.tmux import TmuxHost, tmux_host, types
from agent_pm.session.hosts.tmux.host import TmuxProcess
from agent_pm.utils import ProcessControl, load_class

from session.tests.case import Case
from session.tests.tmux_host_support import LINE, PROCESS, Base

TuiError = types.TuiError
NO_PANE = "no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)"
FOLDS = (
    (("mgr", None, None), "mgr"),
    (("mgr", "right", None), "mgr/right"),
    (("w0t0p0:ABC-1", "below", "m1"), "w0t0p0:ABC-1/below/m1"),
    ((None, "below", None), "/below"),
    ((None, None, "m1"), "//m1"),
    ((None, "right", "m1"), "/right/m1"),
    ((None, None, None), None),
)
FOREIGN = ("elsewhere:1.2", "mgr/left", "a b", "mgr/right/a b", "a/b/c/d", "x:y:z")


class ClassTest(Case):
    def test_a_displayable_host_loaded_by_its_config_name(self):
        cls = load_class("agent_pm.session.hosts.tmux:TmuxHost", Host)
        self.assertIs(cls, TmuxHost)
        self.assertTrue(issubclass(cls, Displayable))
        host = TmuxHost("tmux", {"status_line": True})
        self.assertEqual(
            (host.name, host.settings, host.tmux), ("tmux", {"status_line": True}, tmux_host)
        )


class FoldTest(Case):
    def test_fold_and_unfold_round_trip(self):
        for parts, display in FOLDS:
            with self.subTest(parts=parts):
                self.assertEqual(TmuxHost.fold(*parts), display)
                self.assertEqual(TmuxHost.unfold(display), parts)

    def test_unfold_takes_empty_parts_as_absent(self):
        for display, parts in (("", (None, None, None)), ("mgr/", ("mgr", None, None))):
            with self.subTest(display=display):
                self.assertEqual(TmuxHost.unfold(display), parts)

    def test_unfold_of_a_display_not_in_the_format_is_none(self):
        for display in (*FOREIGN, "mgr\t/right", "mgr;", "mgr/right;", 5):
            with self.subTest(display=display):
                self.assertIsNone(TmuxHost.unfold(display))

    def test_fold_refuses_a_bad_part_with_the_drivers_messages(self):
        for parts, message in (
            (
                ("x y", None, None),
                "opener must be a tmux session name or $ITERM_SESSION_ID's value, got 'x y'",
            ),
            (
                ("w0t0p0:a/b", None, None),
                "opener must be a tmux session name or $ITERM_SESSION_ID's value, got 'w0t0p0:a/b'",
            ),
            ((None, "left", None), "split must be right or below, got 'left'"),
            ((None, None, "a b"), "split_from must match [A-Za-z0-9_-]+, got 'a b'"),
            ((None, None, 5), "split_from must match [A-Za-z0-9_-]+, got 5"),
        ):
            with self.subTest(parts=parts), self.assertRaises(ConfigError) as caught:
                TmuxHost.fold(*parts)
            self.assertEqual(str(caught.exception), message)


class DisplayTest(Base):
    def test_the_callers_opener_fills_an_empty_or_absent_opener_part(self):
        for value, want in (
            (None, "mgr"),
            ("", "mgr"),
            ("/right", "mgr/right"),
            ("//m1", "mgr//m1"),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.host.display(value), want)

    def test_a_given_opener_or_a_foreign_value_is_returned_as_given(self):
        for value in ("w1", "w1/below/m1", *FOREIGN):
            with self.subTest(value=value):
                self.assertEqual(self.host.display(value), value)
        self.tmux.opener.assert_not_called()

    def test_no_opener_available_returns_the_value_unchanged(self):
        self.tmux.opener.side_effect = TuiError(NO_PANE)
        for value in (None, "", "/right"):
            with self.subTest(value=value):
                self.assertEqual(self.host.display(value), value)

    def test_a_display_tmux_could_not_list_is_a_config_error(self):
        for value in ("mgr\tx", "mgr\nx", "mgr;", 5):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                self.host.display(value)
            self.assertEqual(str(caught.exception), tmux_host.display_error(value))
        self.tmux.opener.assert_not_called()

    def test_display_help_names_the_format(self):
        help_text = self.host.display_help()
        self.assertIn("<opener>[/<split>[/<split_from>]]", help_text)
        self.assertIn("right or below", help_text)


class DetachTest(Base):
    def test_tmux_failure_removes_the_launch_without_killing_an_existing_name(self):
        paths = []

        def refuse(*_args, **kw):
            paths.append(kw["control"])
            raise TuiError("session d1 exists")

        self.tmux.detach.side_effect = refuse
        with self.assertRaisesRegex(DriverError, "session d1 exists"):
            self.detach()
        self.assertEqual(len(paths), 1)
        self.assertFalse(os.path.exists(paths[0]))
        self.tmux.kill.assert_not_called()

    def test_startup_timeout_cancels_launch_and_kills_only_the_captured_pane(self):
        paths = []

        def pending(*_args, **kw):
            paths.append(kw["control"])
            return PROCESS

        self.tmux.detach.side_effect = pending
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(DriverError, "process controller did not start"),
        ):
            self.detach()
        self.assertFalse(os.path.exists(paths[0]))
        self.tmux.kill.assert_called_once_with("%7")

    def test_a_cancellation_socket_error_is_a_driver_error(self):
        def refuse(*_args, **kw):
            control = ProcessControl(kw["control"])
            self.addCleanup(control.remove)
            Path(control.path, "control").touch()
            raise TuiError("cannot create pane")

        self.tmux.detach.side_effect = refuse
        self.connection.connect.side_effect = TimeoutError("cancel timed out")
        with self.assertRaisesRegex(
            DriverError, "cannot create pane; cancel controller: cancel timed out"
        ):
            self.detach()
        self.tmux.kill.assert_not_called()

    def test_a_cancellation_deadline_is_a_driver_error(self):
        def refuse(*_args, **kw):
            control = ProcessControl(kw["control"])
            self.addCleanup(control.remove)
            Path(control.path, "control").touch()
            raise TuiError("cannot create pane")

        self.tmux.detach.side_effect = refuse
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(
                DriverError, "cancel controller: process controller did not cancel"
            ),
        ):
            self.detach()
        self.tmux.kill.assert_not_called()

    def test_a_startup_cleanup_tmux_error_is_a_driver_error(self):
        self.tmux.detach.side_effect = lambda *_args, **_kw: PROCESS
        self.tmux.process.side_effect = TuiError("cannot inspect pane")
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(
                DriverError, "process controller did not start; close pane: cannot inspect pane"
            ),
        ):
            self.detach()
        self.tmux.kill.assert_not_called()

    def test_failed_cancellation_still_closes_only_the_owned_pane(self):
        def pending(*_args, **kw):
            control = ProcessControl(kw["control"])
            self.addCleanup(control.remove)
            Path(control.path, "control").touch()
            return PROCESS

        self.tmux.detach.side_effect = pending
        self.connection.connect.side_effect = PermissionError("controller denied")
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(DriverError, "cancel controller: controller denied"),
        ):
            self.detach()
        self.tmux.kill.assert_called_once_with("%7")

    def detach(self, **kw):
        return self.host.detach(
            "d1", Launch((self.tool, "a b"), cwd=self.dir, env={"PATH": "/bin"}), **kw
        )

    def test_forwards_the_launch_the_opener_part_and_the_record_line(self):
        events = os.path.join(self.dir, "events")
        process = self.detach(display="mgr/below", events=events, record=LINE)
        self.tmux.detach.assert_called_once_with(
            "d1",
            Launch(tuple(self.control.command()), cwd=self.dir, env={"PATH": "/bin"}),
            opener="mgr",
            display="mgr/below",
            events=events,
            record=LINE.to_line(),
            control=self.control.path,
        )
        self.assertEqual((process.name, process.alive()), ("d1", True))
        self.assertEqual(
            json.loads(Path(self.control.path, "launch.json").read_text()),
            {
                "argv": [self.tool, "a b"],
                "cwd": self.dir,
                "env": {"PATH": "/bin"},
            },
        )
        self.tmux.status.assert_called_with("%7")
        self.assertEqual(SIGNALS, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))

    def test_a_foreign_or_openerless_display_is_stored_without_an_opener(self):
        for display in (None, "", "/right", "elsewhere:1.2"):
            with self.subTest(display=display):
                self.tmux.detach.reset_mock()
                self.detach(display=display)
                kw = self.tmux.detach.call_args.kwargs
                self.assertEqual((kw["opener"], kw["display"], kw["record"]), (None, display, None))

    def test_a_display_tmux_could_not_list_or_a_bad_events_file_is_a_config_error(self):
        target, link = os.path.join(self.dir, "target"), os.path.join(self.dir, "link")
        open(target, "w").close()
        os.symlink(target, link)
        for kw, message in (
            ({"display": "mgr\tx"}, r"^display 'mgr\\tx': "),
            ({"display": "mgr\n"}, "^display "),
            ({"display": "mgr;"}, "^display "),
            ({"events": link}, f"^events file {re.escape(link)}: "),
        ):
            with self.subTest(**kw), self.assertRaisesRegex(ConfigError, message):
                self.detach(**kw)
        self.tmux.detach.assert_not_called()

    def test_a_tmux_failure_is_a_driver_error(self):
        self.tmux.detach.side_effect = TuiError("session d1 exists")
        with self.assertRaises(DriverError) as caught:
            self.detach()
        self.assertEqual(str(caught.exception), "detach d1: session d1 exists")


class TerminalTest(Base):
    def test_terminal_launch_has_a_controller_identity_and_keeps_its_metadata(self):
        events = os.path.join(self.dir, "events")
        launch = Launch((self.tool, "a b"), cwd=self.dir, env={"PATH": "/bin", "TERM": "exact"})
        terminal = self.host.terminal("t1", launch, events=events, display="mgr/below", record=LINE)
        self.assertIsInstance(terminal, Terminal)
        self.assertEqual(terminal.identity, PROCESS)
        self.tmux.start.assert_called_once_with(
            "t1",
            Launch(tuple(self.control.command()), cwd=self.dir, env=launch.env),
            events=events,
            status_line=False,
            tags={
                "@kind": "terminal",
                "@control": self.control.path,
                "@display": "mgr/below",
                "@opener": "mgr",
                "@record": LINE.to_line(),
            },
        )
        terminal.send(Paste("a b"))
        self.tmux.paste.assert_called_once_with("%7", "a b")
        self.assertEqual(
            json.loads(Path(self.control.path, "launch.json").read_text())["env"], dict(launch.env)
        )

    def test_terminal_with_no_controller_is_refused_when_attaching(self):
        self.kind, self.controlled = "terminal", False
        with self.assertRaisesRegex(DriverError, "missing @control"):
            self.host.attach("t1")


class DisplayableTest(Base):
    def test_show_opens_a_pane_per_the_unfolded_display_and_the_grid_settings(self):
        self.tmux.open_pane.return_value = None
        self.host.settings["grid_retile"] = "all"
        self.assertIsNone(self.host.show("w1", display="mgr/below/m1"))
        self.tmux.open_pane.assert_called_once_with(
            "w1", split="below", split_from="m1", opener="mgr", per_column=2, retile="all"
        )

    def test_show_without_a_pane_logs_why_and_returns_the_attach_command(self):
        self.tmux.open_pane.return_value = NO_PANE
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(self.host.show("w1"), "tmux attach -t '=w1'")
        self.assertEqual(logs.output, [f"WARNING:session:show w1: {NO_PANE}"])
        self.tmux.open_pane.assert_called_once_with(
            "w1", split=None, split_from=None, opener=None, per_column=2, retile=None
        )

    def test_show_refuses_an_absent_session_a_foreign_display_or_a_bad_setting(self):
        self.tmux.status.return_value = None
        with self.assertRaisesRegex(DriverError, "^no tmux session w1$"):
            self.host.show("w1")
        self.tmux.status.return_value = types.PaneStatus.RUNNING
        with self.assertRaisesRegex(ConfigError, r"^display 'elsewhere:1\.2': not <opener>"):
            self.host.show("w1", display="elsewhere:1.2")
        self.tmux.open_pane.side_effect = TuiError(
            "per_column must be an int from 1 to 9999, not 0"
        )
        with self.assertRaisesRegex(ConfigError, "^per_column must be"):
            self.host.show("w1")

    def test_layout_completes_the_display_once_the_pane_is_found(self):
        for display, calls, want in (
            (None, [mock.call.anchor(None), mock.call.opener()], "mgr"),
            ("/right", [mock.call.anchor(None), mock.call.opener()], "mgr/right"),
            ("w1/below", [], "w1/below"),
            (
                "/below/m1",
                [mock.call.status("m1"), mock.call.anchor("m1"), mock.call.opener()],
                "mgr/below/m1",
            ),
        ):
            with self.subTest(display=display):
                self.tmux.reset_mock()
                self.assertEqual(self.host.layout(display=display), want)
                self.assertEqual(self.tmux.mock_calls, calls)

    def test_layout_beside_split_from_without_an_opener_keeps_the_part_empty(self):
        self.tmux.opener.side_effect = TuiError(NO_PANE)
        self.assertEqual(self.host.layout(display="//m1"), "//m1")

    def test_layout_without_a_pane_or_split_from_is_refused(self):
        hint = ": run from tmux or iTerm2, or pass split_from"
        self.tmux.anchor.side_effect = TuiError(NO_PANE)
        with self.assertRaises(DriverError) as caught:
            self.host.layout()
        self.assertEqual(str(caught.exception), f"no pane to show the TUI beside ({NO_PANE}){hint}")
        self.tmux.status.return_value = None
        with self.assertRaises(ConfigError) as caught:
            self.host.layout(display="//m1")
        self.assertEqual(str(caught.exception), "no tmux session m1")

    def test_placement_and_the_tmux_module_are_keyword_only(self):
        for call in (
            lambda: self.host.show("w1", "mgr/below"),
            lambda: self.host.layout("/below"),
            lambda: TmuxProcess("w1", PROCESS, self.tmux),
        ):
            with self.subTest(call=call), self.assertRaises(TypeError):
                call()
        self.assertIs(
            TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control).tmux, self.tmux
        )

    def test_attach_command_screen_and_tile_forward(self):
        self.assertEqual(self.host.attach_command("w1"), "tmux attach -t '=w1'")
        self.tmux.read.return_value = "text"
        self.assertEqual(self.host.screen("w1", 5), "text")
        self.tmux.read.assert_called_once_with("w1", 5)
        self.host.tile("@1")
        self.tmux.tile.assert_called_once_with("@1")
        self.tmux.read.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(DriverError, "^tmux session w1: tmux: boom$"):
            self.host.screen("w1")
        with self.assertRaisesRegex(DriverError, "^no tmux session 'a b'$"):
            self.host.attach_command("a b")
