"""TmuxHost launch/display boundaries and process-close ordering over recorded calls.
Attachment trust uses real metadata in integration/hosts/tmux/process_integration_test.py.
"""

import errno
import json
import logging
import os
import re
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest import mock

from agent_pm.session import ConfigError, Displayable, Host, Launch, SessionError, Terminal
from agent_pm.session.engine.types import ActivityState, Key, KeyPress, Paste, SessionRow
from agent_pm.session.hosts.base import SIGNALS
from agent_pm.session.hosts.tmux import TmuxHost, transport, types
from agent_pm.session.hosts.tmux.host import (
    EXEC,
    EXITED_EVENT,
    HANDOVER_TIMEOUT,
    POLL,
    TERMINAL_KEYS,
    Tmux,
    TmuxProcess,
    TmuxTerminal,
    events_file,
    validate_events,
)
from agent_pm.utils import ProcessControl, load_class

from session.tests.case import Case
from session.tests.hosted_support import SID, listed
from session.tests.tmux_fixtures import (
    EXITED,
    EXITED_HOOK,
    FAILED,
    FORMAT,
    INSIDE,
    ITERM,
    NEW,
    NEW_PROCESS,
    PREFIXES,
    TMUX_KW,
    FakeTmux,
    StartFixture,
    chained,
    decorations,
    environ,
    recorded,
    which,
)
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
        self.assertEqual((host.name, host.settings), ("tmux", {"status_line": True}))
        self.assertIsInstance(host.tmux, Tmux)
        self.assertEqual(
            (f"{host.tmux.proc.__module__}.{host.tmux.proc.__name__}", host.tmux.sleep),
            ("subprocess.run", time.sleep),
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
            self.assertEqual(str(caught.exception), transport.display_error(value))
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
        with self.assertRaises(SessionError) as caught:
            self.detach()
        self.assertEqual(str(caught.exception), "detach d1: session d1 exists")
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
            self.assertRaisesRegex(SessionError, "process controller did not start"),
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
            SessionError, "cannot create pane; cancel controller: cancel timed out"
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
                SessionError, "cancel controller: process controller did not cancel"
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
                SessionError, "process controller did not start; close pane: cannot inspect pane"
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
            self.assertRaisesRegex(SessionError, "cancel controller: controller denied"),
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

    def test_only_an_explicit_launch_mask_is_applied_to_the_target(self):
        self.host.detach(
            "d1",
            Launch((self.tool,), cwd=self.dir, blocked_signals=SIGNALS),
        )
        self.assertEqual(
            json.loads(Path(self.control.path, "launch.json").read_text())["blocked_signals"],
            list(SIGNALS),
        )

    def test_a_foreign_or_openerless_display_is_stored_without_an_opener(self):
        for display in (None, "", "/right", "elsewhere:1.2"):
            with self.subTest(display=display):
                self.tmux.detach.reset_mock()
                self.detach(display=display)
                kw = self.tmux.detach.call_args.kwargs
                self.assertEqual((kw["opener"], kw["display"], kw["record"]), (None, display, None))

    def test_a_display_tmux_could_not_list_or_a_bad_events_file_is_a_config_error(self):
        for kw, message in (
            ({"display": "mgr\tx"}, r"^display 'mgr\\tx': "),
            ({"display": "mgr\n"}, "^display "),
            ({"display": "mgr;"}, "^display "),
            ({"events": self.dir}, f"^events file {re.escape(self.dir)}: "),
        ):
            with self.subTest(**kw), self.assertRaisesRegex(ConfigError, message):
                self.detach(**kw)
        self.tmux.detach.assert_not_called()

    def test_events_creation_failure_is_a_driver_error_before_launch(self):
        self.tmux.events_file.side_effect = TuiError("cannot create events")
        with self.assertRaisesRegex(SessionError, "cannot create events"):
            self.detach(events=os.path.join(self.dir, "events"))
        self.tmux.detach.assert_not_called()
        self.socket.assert_not_called()


class TerminalTest(Base):
    def test_a_terminal_that_never_starts_closes_only_its_captured_pane(self):
        paths = []

        def pending(*_args, **kw):
            paths.append(kw["tags"]["@control"])
            return PROCESS

        self.tmux.start.side_effect = pending
        with (
            mock.patch("time.monotonic", side_effect=[0, 11]),
            self.assertRaisesRegex(SessionError, "process controller did not start"),
        ):
            self.host.terminal("t1", Launch((self.tool,), cwd=self.dir))
        self.assertFalse(os.path.exists(paths[0]))
        self.tmux.kill.assert_called_once_with("%7")

    def test_only_a_key_following_a_paste_waits_for_paste_delivery(self):
        self.active(*[{"live": True}] * 4)
        terminal = TmuxTerminal("t1", PROCESS, tmux=self.tmux, control=self.control)
        order = mock.Mock()
        order.attach_mock(self.tmux.paste, "paste")
        order.attach_mock(self.tmux.keys, "keys")
        with mock.patch("time.sleep") as sleep:
            order.attach_mock(sleep, "sleep")
            terminal.send(Key("Escape"))
            terminal.send(Paste("a b"))
            terminal.send(Key("Enter"))
            terminal.send(Key("Escape"))
        self.assertEqual(
            order.mock_calls,
            [
                mock.call.keys("%7", "Escape"),
                mock.call.paste("%7", "a b"),
                mock.call.sleep(self.tmux.PAUSE),
                mock.call.keys("%7", "Enter"),
                mock.call.keys("%7", "Escape"),
            ],
        )

    def test_press_translates_every_key_then_sends_them_in_one_tmux_call(self):
        self.active()
        for notation, names in (
            (
                ("Escape", "3", "PageUp", "PageDown", " ", ";", "C-M-S-Up", "-", "BTab", "C-c"),
                ("Escape", "3", "PPage", "NPage", "Space", "\\;", "C-M-S-Up", "-", "BTab", "C-c"),
            ),
            # ctrl reaches letters, space and the C0 punctuation
            (
                ("C- ", "C-/", "C-@", "C-[", "C-\\", "C-]", "C-^", "C-_", "C-?", "C-M-[", "C-A"),
                (
                    "C-Space",
                    "C-/",
                    "C-@",
                    "C-[",
                    "C-\\",
                    "C-]",
                    "C-^",
                    "C-_",
                    "C-?",
                    "C-M-[",
                    "C-A",
                ),
            ),
        ):
            with self.subTest(notation=notation):
                self.tmux.press.reset_mock()
                terminal = TmuxTerminal("t1", PROCESS, tmux=self.tmux, control=self.control)
                terminal.press(tuple(map(KeyPress.parse, notation)))
                self.tmux.press.assert_called_once_with("%7", *names)

    def test_a_key_it_cannot_translate_sends_none_of_the_keys(self):
        self.active()
        terminal = TmuxTerminal("t1", PROCESS, tmux=self.tmux, control=self.control)
        for bad in ("Up", *map(KeyPress.parse, ("C-1", "C-2", "C--", "C-;", "C-{", "S-a"))):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                terminal.press((KeyPress("Up"), bad))
        self.tmux.press.assert_not_called()
        terminal.press((KeyPress.parse("C-c"), KeyPress.parse("S-Up"), KeyPress.parse("M-1")))
        self.tmux.press.assert_called_once_with("%7", "C-c", "S-Up", "M-1")

    def test_a_press_following_a_paste_waits_for_paste_delivery(self):
        self.active()
        terminal = TmuxTerminal("t1", PROCESS, tmux=self.tmux, control=self.control)
        with mock.patch("time.sleep") as sleep:
            terminal.send(Paste("a b"))
            terminal.press((KeyPress("Enter"),))
        sleep.assert_called_once_with(self.tmux.PAUSE)

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

    def test_a_terminal_is_the_pane_its_start_made_not_the_sessions_active_one(self):
        self.tmux.process.side_effect = lambda target: (
            PROCESS if target == "%7" else "4242 $3 %9 5555"
        )
        terminal = self.host.terminal("t1", Launch((self.tool,), cwd=self.dir))
        self.assertEqual(terminal.identity, PROCESS)
        terminal.send(Paste("a b"))
        self.tmux.paste.assert_called_once_with("%7", "a b")

    def test_terminal_with_no_controller_is_refused_when_attaching(self):
        self.kind, self.controlled = "terminal", False
        with self.assertRaisesRegex(SessionError, "missing @control"):
            self.host.attach("t1")


class AttachTest(Base):
    def setUp(self):
        super().setUp()
        self.tmux.process.side_effect = lambda target: (
            "4242 $3 %9 5555" if target == "w1" else PROCESS if target == "%7" else None
        )

    def test_a_launched_session_without_a_valid_process_option_is_refused(self):
        for value, error in (
            (None, "^tmux session w1: missing @process; close sessions before upgrading$"),
            ("4242 $3 %9", r"^tmux session w1: bad @process '4242 \$3 %9'$"),
        ):
            with self.subTest(value=value):
                self.recorded = value
                with self.assertRaisesRegex(SessionError, error):
                    self.host.attach("w1")

    def test_a_foreign_session_is_its_active_pane(self):
        self.kind, self.controlled = None, False
        handle = self.host.attach("w1")
        self.assertIs(type(handle), TmuxProcess)
        self.assertEqual(handle.identity, "4242 $3 %9 5555")


class DisplayableTest(Base):
    def test_show_opens_a_pane_per_the_unfolded_display_and_the_grid_settings(self):
        self.tmux.open_pane.return_value = None
        self.host.settings["grid_retile"] = "all"
        self.assertIsNone(self.host.show("w1", display="mgr/below/m1"))
        self.tmux.open_pane.assert_called_once_with(
            "w1", ("mgr", "below", "m1"), per_column=2, retile="all"
        )

    def test_show_without_a_pane_logs_why_and_returns_the_attach_command(self):
        self.tmux.open_pane.return_value = NO_PANE
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(self.host.show("w1"), "tmux attach -t '=w1'")
        self.assertEqual(logs.output, [f"WARNING:session:show w1: {NO_PANE}"])
        self.tmux.open_pane.assert_called_once_with(
            "w1", (None, None, None), per_column=2, retile=None
        )

    def test_show_with_a_command_runs_it_instead_of_opening_a_pane(self):
        self.tmux.run_command.return_value = None
        self.assertIsNone(self.host.show("w1", command="open -a T {{session}}"))
        self.tmux.run_command.assert_called_once_with("w1", "open -a T {{session}}")
        self.tmux.open_pane.assert_not_called()
        self.tmux.run_command.return_value = "exit status 3"
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(self.host.show("w1", command="false"), "tmux attach -t '=w1'")
        self.assertEqual(logs.output, ["WARNING:session:show w1: exit status 3"])
        self.tmux.run_command.return_value = None
        self.assertIsNone(self.host.show("w1", display="elsewhere:1.2", command="true"))
        self.tmux.run_command.assert_called_with("w1", "true")
        self.tmux.open_pane.assert_not_called()

    def test_show_refuses_an_absent_session_a_foreign_display_or_a_bad_setting(self):
        self.tmux.status.return_value = None
        with self.assertRaisesRegex(SessionError, "^no tmux session w1$"):
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
        with self.assertRaises(SessionError) as caught:
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
        self.tmux.read.assert_called_once_with("%7", 5)
        self.host.tile("@1")
        self.host.tile("@2", wait=False, if_panes_changed=True)
        self.assertEqual(
            self.tmux.tile.call_args_list,
            [
                unittest.mock.call("@1", wait=True, if_panes_changed=False),
                unittest.mock.call("@2", wait=False, if_panes_changed=True),
            ],
        )
        self.tmux.read.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
            self.host.screen("w1")
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.host.attach_command("a b")

    def test_screen_of_a_name_reads_its_launched_pane_else_its_active_one(self):
        self.tmux.read.return_value = "text"
        self.recorded = None
        self.assertEqual(self.host.screen("w1"), "text")
        self.tmux.read.assert_called_once_with("w1", None)
        self.recorded = "4242 $3 %x 4343"
        with self.assertRaisesRegex(
            SessionError, "^tmux session w1: bad @process '4242 \\$3 %x 4343'$"
        ):
            self.host.screen("w1")
        self.tmux.option.side_effect = TuiError("tmux: lost server")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: lost server$"):
            self.host.screen("w1")
        self.assertEqual(self.tmux.read.call_count, 1)
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.host.screen("a b")


class PublishTest(Base):
    def test_publish_sets_the_sessions_record_option_while_its_pane_is_its_own(self):
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        process.publish(LINE)
        self.tmux.set_option.assert_called_once_with("%7", "@record", LINE.to_line())
        self.assertEqual(process.token, self.control.path)
        self.tmux.process.return_value = "9 $1 %7 9"
        with self.assertRaisesRegex(SessionError, "^tmux session w1 is gone$"):
            process.publish(LINE)
        with self.assertRaises(ConfigError):
            process.publish(LINE.to_line())
        self.assertEqual(self.tmux.set_option.call_count, 1)


class DescendantsTest(Base):
    def test_the_matching_sessions_then_those_each_opened_transitively(self):
        self.tmux.listing.return_value = [
            listed("a", opener="mgr", display="mgr"),
            listed("b", opener="a", display="a/below"),
            listed("c", opener="b", display="b"),
            listed("d", opener="d", display="d"),
            listed("x", opener="other", display="other"),
        ]
        self.assertEqual(self.host.list("mgr"), ["a"])
        self.assertEqual(self.host.descendants("mgr"), ["a", "b", "c"])
        self.assertEqual(self.host.descendants("other"), ["x"])
        self.assertEqual(self.host.descendants(""), [])

    def test_a_tmux_failure_is_a_driver_error(self):
        self.tmux.listing.side_effect = [[listed("a", opener="mgr", display="mgr")], None]
        with self.assertRaisesRegex(SessionError, "tmux failed"):
            self.host.descendants("mgr")


class RowsTest(Base):
    def test_a_row_per_listed_session_in_its_order(self):
        self.tmux.listing.return_value = [
            listed("mgr"),
            listed("w1", sid=SID, client="fake", state="done", display="mgr/below", shown="m"),
            listed("w2", sid="a/b", client="Fake", state="blocked", shown=""),
            listed("w3", client="x-y", state="dead"),
            listed("w4", client="fake", state="done", dead=True, last_lines="bye"),
            listed("w5", client="fake9_", state="other", dead=None),
        ]
        self.tmux.own_session.return_value = "mgr"
        rows = self.host.rows()
        self.assertEqual([type(r.state) for r in rows], [ActivityState] * 6)
        row = SessionRow
        self.assertEqual(
            rows,
            [
                row(
                    name="mgr",
                    sid=None,
                    client=None,
                    state="working",
                    display=None,
                    shown=None,
                    last_lines=None,
                    own=True,
                ),
                row(
                    name="w1",
                    sid=SID,
                    client="fake",
                    state="done",
                    display="mgr/below",
                    shown="m",
                    last_lines=None,
                    own=False,
                ),
                row(
                    name="w2",
                    sid=None,
                    client=None,
                    state="blocked",
                    display=None,
                    shown="",
                    last_lines=None,
                    own=False,
                ),
                row(
                    name="w3",
                    sid=None,
                    client=None,
                    state="working",
                    display=None,
                    shown=None,
                    last_lines=None,
                    own=False,
                ),
                row(
                    name="w4",
                    sid=None,
                    client="fake",
                    state="dead",
                    display=None,
                    shown=None,
                    last_lines="bye",
                    own=False,
                ),
                row(
                    name="w5",
                    sid=None,
                    client="fake9_",
                    state="working",
                    display=None,
                    shown=None,
                    last_lines=None,
                    own=False,
                ),
            ],
        )
        self.assertEqual(
            self.tmux.mock_calls,
            [mock.call.listing(), mock.call.listing(), mock.call.own_session()],
        )


class CloseTest(Base):
    def test_a_failed_pane_kill_still_terminates_the_owned_process_group(self):
        self.active({"live": True}, {"live": False})
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        self.tmux.kill.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^tmux: boom$"):
            process.close()
        self.assertEqual(
            self.connection.sendall.call_args_list,
            [mock.call(b"signal 15\n"), mock.call(b"live\n")],
        )

    def test_close_during_controller_retirement_never_sends_a_numeric_group_signal(self):
        self.active()
        Path(self.control.path, "control").touch(mode=0o600)
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        for failure in (ConnectionRefusedError, ConnectionResetError):
            with self.subTest(failure=failure):
                self.connection.connect.side_effect = failure
                process.close()
        self.killpg.assert_not_called()
        self.connection.sendall.assert_not_called()
        self.assertFalse(self.control.ended)

    def test_a_denied_termination_preserves_the_controller_for_later_cleanup(self):
        self.active()
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        self.connection.connect.side_effect = PermissionError("termination denied")
        with self.assertRaisesRegex(SessionError, "process group w1: termination denied"):
            process.close()
        self.assertFalse(self.control.ended)
        self.killpg.assert_not_called()

    def test_a_failed_pane_kill_and_a_denied_termination_both_reach_the_error(self):
        self.active()
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        self.tmux.kill.side_effect = TuiError("tmux: boom")
        self.connection.connect.side_effect = PermissionError("termination denied")
        with self.assertRaises(SessionError) as caught:
            process.close()
        self.assertEqual(str(caught.exception), "tmux: boom; process group w1: termination denied")

    def test_attach_wraps_a_failed_kind_read_as_a_driver_error(self):
        self.tmux.option.side_effect = TuiError("tmux: lost server")
        with self.assertRaises(SessionError) as caught:
            self.host.attach("t1")
        self.assertEqual(str(caught.exception), "tmux session t1: tmux: lost server")

    def test_close_removes_the_pane_then_allows_term_cleanup_without_hard_kill(self):
        self.active({"live": True}, {"live": True}, {"live": False})
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        order = mock.Mock()
        order.attach_mock(self.tmux.kill, "pane")
        order.attach_mock(self.connection.sendall, "request")
        with mock.patch("time.sleep") as sleep:
            process.close()
        self.assertEqual(
            order.mock_calls,
            [
                mock.call.pane("%7"),
                mock.call.request(b"signal 15\n"),
                mock.call.request(b"live\n"),
                mock.call.request(b"live\n"),
            ],
        )
        sleep.assert_called_once_with(0.1)

    def test_close_forces_survivors_only_after_the_grace_period(self):
        self.active({"live": True})
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        clock, requests, killed = [0.0], [], [False]

        def request(command):
            requests.append((command, clock[0]))
            if command == b"kill\n":
                killed[0] = True

        self.connection.sendall.side_effect = request
        stream = self.connection.makefile.return_value.__enter__.return_value
        stream.readline.side_effect = lambda: json.dumps({"live": not killed[0]}).encode() + b"\n"
        with (
            mock.patch("time.monotonic", side_effect=lambda: clock[0]),
            mock.patch(
                "time.sleep", side_effect=lambda _seconds: clock.__setitem__(0, clock[0] + 1)
            ),
        ):
            process.close()
        self.assertEqual(requests[0], (b"signal 15\n", 0))
        self.assertEqual(
            [request for request in requests if request[0] == b"kill\n"], [(b"kill\n", 5)]
        )

    def test_close_reports_a_group_that_survives_the_forced_kill_without_forgetting_it(self):
        self.active({"live": True})
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        stream = self.connection.makefile.return_value.__enter__.return_value
        stream.readline.side_effect = None
        clock = [0.0]
        with (
            mock.patch("time.monotonic", side_effect=lambda: clock[0]),
            mock.patch(
                "time.sleep", side_effect=lambda _seconds: clock.__setitem__(0, clock[0] + 1)
            ),
            self.assertRaisesRegex(SessionError, "process group w1: did not exit"),
        ):
            process.close()
        self.assertEqual(clock[0], 10)
        self.assertFalse(self.control.ended)
        self.assertEqual(self.connection.sendall.call_args_list[0], mock.call(b"signal 15\n"))


class ReturncodeTest(Base):
    def test_the_captured_exit_status_survives_pane_removal_immediately_afterward(self):
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        self.tmux.process.side_effect = [PROCESS, TuiError("bad tmux process '4242  %7 4343'")]
        self.tmux.status.return_value = 7
        self.assertEqual(process.returncode(), 7)

    def test_absence_unknown_and_running_status_are_distinct(self):
        process = TmuxProcess("w1", PROCESS, tmux=self.tmux, control=self.control)
        for identity, status, expected in (
            (None, types.PaneStatus.RUNNING, -signal.SIGHUP),
            (PROCESS, None, -signal.SIGHUP),
            (PROCESS, types.PaneStatus.UNKNOWN, None),
            (PROCESS, types.PaneStatus.RUNNING, None),
            (PROCESS, 0, 0),
        ):
            with self.subTest(identity=identity, status=status):
                self.tmux.process.return_value = identity
                self.tmux.status.return_value = status
                self.assertEqual(process.returncode(), expected)
        self.tmux.process.side_effect = TuiError("tmux unavailable")
        self.assertIsNone(process.returncode())


class DetachHandoverTest(StartFixture):
    """Session d hosting argv as old drive.detach did: no decoration, no dead pane kept."""

    def detach(self, fake, session="d", argv=("tool", "-x", "a b"), env=None, **kw):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        return Tmux(fake, self.sleep).detach(session, Launch(tuple(argv), env, cwd=self.cwd), **kw)

    def test_new_session_runs_the_wrapper_and_returns_its_process_once_argv_execd(self):
        fake = FakeTmux()
        self.assertEqual(self.detach(fake), NEW_PROCESS)
        self.assertEqual(
            fake.calls,
            [
                [
                    "tmux",
                    "new-session",
                    "-d",
                    *NEW,
                    "-s",
                    "d",
                    sys.executable,
                    "-I",
                    "-c",
                    EXEC,
                    fake.path,
                    fake.fifo,
                    ";",
                    "set-option",
                    "-t",
                    "=d:",
                    "@kind",
                    "detach",
                    *recorded("d"),
                ]
            ],
        )
        self.assertEqual(fake.kwargs, [TMUX_KW])
        self.assertNotIn("remain-on-exit", fake.calls[0])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_the_wrapper_code_tmux_runs_is_safe_for_tmux(self):
        fake = FakeTmux()
        self.detach(fake)
        call = fake.calls[0]
        code = call[call.index(sys.executable) + 3]
        self.assertEqual(code, EXEC)
        self.assertNotIn("#", code)
        self.assertFalse(code.rstrip().endswith(";"))

    def test_handover_file_without_the_terminal_keys(self):
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in TERMINAL_KEYS},
        }
        fake = FakeTmux()
        self.detach(fake, env=env)
        self.assertEqual(
            fake._handover,
            {
                "argv": [self.tool, "-x", "a b"],
                "cwd": self.cwd,
                "env": {"PATH": self.bin, "HOME": "/h", "CHILD_SESSION": "1", "PWD": self.cwd},
            },
        )
        self.assertEqual(fake.mode, 0o600)
        self.assertEqual(fake.dir_mode, 0o700)
        self.assertEqual(os.listdir(self.temp), [])

    def test_the_envs_iterm_session_id_goes_to_the_session_only_in_its_iterm2_form(self):
        for value, given in (
            ("w0t0p0:ABC", True),
            ("w0t1p0:6D7E-8F", True),
            (None, False),
            ("", False),
            ("w0t0p0", False),
            ("cmd", False),
            ("w0t0p0:a;b", False),
            ("w0t0p0:ABC\n", False),
            (":ABC", False),
            ("w0t0p0:A_B", False),
        ):
            with self.subTest(value=value):
                fake = FakeTmux()
                env = {"PATH": self.bin, **({} if value is None else {"ITERM_SESSION_ID": value})}
                self.detach(fake, env=env)
                head = [
                    "tmux",
                    "new-session",
                    "-d",
                    *NEW,
                    *(["-e", f"ITERM_SESSION_ID={value}"] if given else []),
                    "-s",
                    "d",
                ]
                self.assertEqual(fake.calls[0][: len(head) + 1], [*head, sys.executable])
                self.assertNotIn("ITERM_SESSION_ID", fake._handover["env"])

    def test_opener_set_in_the_same_call(self):
        for opener in ("mgr", "w0t0p0:ABC"):
            with self.subTest(opener=opener):
                fake = FakeTmux()
                self.detach(fake, opener=opener)
                self.assertEqual(
                    fake.calls,
                    [
                        [
                            "tmux",
                            "new-session",
                            "-d",
                            *NEW,
                            "-s",
                            "d",
                            sys.executable,
                            "-I",
                            "-c",
                            EXEC,
                            fake.path,
                            fake.fifo,
                            ";",
                            "set-option",
                            "-t",
                            "=d:",
                            "@kind",
                            "detach",
                            ";",
                            "set-option",
                            "-t",
                            "=d:",
                            "@opener",
                            opener,
                            *recorded("d"),
                        ]
                    ],
                )

    def test_events_keep_the_pane_for_the_exited_hook_and_tags_set_in_the_same_call(self):
        events, line = os.path.join(self.root, "events"), '{"v":1,"name":"d"}'
        fake = FakeTmux()
        self.detach(fake, opener="mgr", display="mgr/right", events=events, record=line)
        self.assertEqual(
            fake.calls[0][fake.calls[0].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-p",
                "-t",
                "=d:",
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@events",
                events,
                ";",
                "set-option",
                "-p",
                "-F",
                "-t",
                "=d:",
                "pane-died",
                EXITED_HOOK,
                ";",
                "set-option",
                "-t",
                "=d:",
                "@kind",
                "detach",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@opener",
                "mgr",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@display",
                "mgr/right",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@record",
                line,
                *recorded("d"),
            ],
        )
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)

    def test_a_display_or_record_is_stored_without_events_and_no_pane_kept(self):
        fake = FakeTmux()
        self.detach(fake, display="elsewhere:1.2", record="{}")
        self.assertEqual(
            fake.calls[0][fake.calls[0].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-t",
                "=d:",
                "@kind",
                "detach",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@display",
                "elsewhere:1.2",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@record",
                "{}",
                *recorded("d"),
            ],
        )

    def test_the_exited_hook_reads_its_values_only_through_tmux_formats_and_kills_its_pane_by_id(
        self,
    ):
        self.assertEqual(EXITED_EVENT, EXITED)
        self.assertEqual(
            set(re.findall(r"#\{[^{}]*\}", EXITED)),
            {
                "#{q:session_name}",
                "#{pane_dead_signal}",
                "#{q:pane_dead_signal}",
                "#{pane_dead_status}",
                "#{q:@events}",
            },
        )
        self.assertEqual(EXITED.count("{"), EXITED.count("#{"))
        self.assertEqual(EXITED.count("{"), EXITED.count("}"))
        hooks = []
        for session, events in (("d", "e1"), ("other-name", "e 2 'x'")):
            fake = FakeTmux()
            self.detach(fake, session=session, events=os.path.join(self.root, events))
            hooks.append(fake.calls[0][fake.calls[0].index("pane-died") + 1])
        self.assertEqual(hooks, [EXITED_HOOK, EXITED_HOOK])

    def test_a_bad_display_record_or_events_is_refused_before_tmux(self):
        for kw, error in (
            ({"display": "mgr\tx"}, "^display 'mgr\\\\tx': want printable text not ending in ;$"),
            ({"display": "mgr\nx"}, "^display "),
            ({"display": "mgr;"}, "^display "),
            ({"display": 5}, "^display 5: "),
            ({"record": "{};"}, "^tag '@record'"),
            ({"record": "a\0b"}, "^tag '@record'"),
            ({"events": self.root}, f"^events file {re.escape(self.root)}: "),
            ({"events": os.path.join(self.root, "a#b")}, "tmux would misread"),
        ):
            with self.subTest(**kw):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, error):
                    self.detach(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_the_creating_call_records_its_pane_identity(self):
        for kw in ({}, {"events": "ev", "opener": "mgr", "record": LINE.to_line()}):
            with self.subTest(**kw):
                fake = FakeTmux()
                if "events" in kw:
                    kw = {**kw, "events": os.path.join(self.root, kw["events"])}
                self.assertEqual(self.detach(fake, **kw), NEW_PROCESS)
                self.assertEqual(fake.calls[0][-len(recorded("d")) :], recorded("d"))

    def test_a_new_session_tmux_does_not_name_is_killed(self):
        for out in ("", "4242 $3 %7\n", "4242 $3 %7 x\n"):
            with self.subTest(out=out):
                fake = FakeTmux(results={"new-session": (0, out, "")})
                with self.assertRaisesRegex(types.TuiError, "^tmux: bad new session "):
                    self.detach(fake)
                self.assertEqual(fake.commands(), ["new-session", "kill-session"])
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_existing_session_is_refused_untouched(self):
        fake = FakeTmux(results={"new-session": (1, "", "duplicate session: d\n")})
        with self.assertRaisesRegex(types.TuiError, r"^session d exists$"):
            self.detach(fake, opener="mgr")
        self.assertEqual(fake.commands(), ["new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_not_taken_kills_it(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = FakeTmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(types.TuiError, r"^session d did not start$"):
                    self.detach(fake)
                self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = FakeTmux(wrapper=errno.ENOENT)
        with self.assertRaisesRegex(
            types.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOENT)}") + "$",
        ):
            self.detach(fake)
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_kills_it(self):
        fake = FakeTmux(wrapper="hang")
        with self.assertRaisesRegex(types.TuiError, r"^session d did not start$"):
            self.detach(fake)
        os.close(fake.writer)
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.assertAlmostEqual(sum(c.args[0] for c in self.sleep.call_args_list), HANDOVER_TIMEOUT)
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = FakeTmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            self.detach(fake, opener="mgr")
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake, self.sleep = FakeTmux(hand_over=False), interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.detach(fake)
        self.assertEqual(fake.commands(), ["new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_launch_rejects_nul_before_tmux(self):
        fake = FakeTmux()
        with self.assertRaisesRegex(ConfigError, "launch: argv must be strings without NUL"):
            self.detach(fake, argv=("tool", "a\0b"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_refused_before_tmux(self):
        for kw, error in (
            ({"argv": ()}, "^no command$"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ("tool", "\ud800")}, "encoded"),
            ({"opener": "a b"}, "opener"),
            ({"opener": "a:b:c"}, "opener"),
            ({"opener": ""}, "opener"),
        ):
            with self.subTest(**kw):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, error):
                    self.detach(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])
        with (
            unittest.mock.patch.object(sys, "executable", "/opt/py#3/bin/python3"),
            self.assertRaisesRegex(types.TuiError, "tmux would misread"),
        ):
            self.detach(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = FakeTmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(types.TuiError, "^handover: "),
        ):
            self.detach(fake)
        self.assertEqual(fake.calls, [])

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = FakeTmux()
        self.detach(fake)
        self.assertEqual(fake._handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])


class EventsFileTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)

    def test_created_0600_and_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        path = os.path.join(self.root, "e")
        self.assertEqual(events_file("e"), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "a") as f:
            f.write("x\n")
        self.assertEqual(events_file(path), path)
        with open(path) as f:
            self.assertEqual(f.read(), "x\n")

    def test_refused(self):
        target = os.path.join(self.root, "target")
        open(target, "w").close()
        for path in (self.root, os.path.join(self.root, "missing", "e")):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(types.TuiError, f"^events file {re.escape(path)}: "),
            ):
                events_file(path)
        fifo = unittest.mock.Mock(st_mode=stat.S_IFIFO | 0o600)
        with (
            unittest.mock.patch("os.fstat", return_value=fifo),
            self.assertRaisesRegex(types.TuiError, "not a regular file$"),
        ):
            events_file(target)

    def test_a_fifo_without_a_reader_is_refused_at_once(self):
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo, 0o600)
        caught = []

        def run():
            for check in (validate_events, events_file):
                try:
                    check(fifo)
                except TuiError as e:
                    caught.append(str(e))

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # lets the blocked open return
            thread.join()
            os.close(reader)
            self.fail("blocked on a FIFO without a reader")
        self.assertEqual(caught, [f"events file {fifo}: not a regular file"] * 2)

    def test_a_parent_that_is_a_file_is_refused_with_nothing_made(self):
        parent = os.path.join(self.root, "file")
        open(parent, "w").close()
        path = os.path.join(parent, "e")
        for check in (validate_events, events_file):
            with self.subTest(check=check.__name__):
                with self.assertRaises(TuiError) as caught:
                    check(path)
                self.assertEqual(str(caught.exception), f"events file {path}: Not a directory")
        self.assertEqual(os.listdir(self.root), ["file"])
        self.assertEqual(os.path.getsize(parent), 0)

    def test_refuses_a_path_tmux_would_misread(self):
        for name in ("a#b", "e;"):
            path = os.path.join(self.root, name)
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    types.TuiError, f"^events file {re.escape(path)}: tmux would misread"
                ),
            ):
                events_file(path)
            self.assertFalse(os.path.exists(path))


class StartTest(StartFixture):
    def test_the_creating_call_records_and_returns_its_pane_identity(self):
        fake = FakeTmux()
        self.assertEqual(self.start(fake), NEW_PROCESS)
        self.assertEqual(fake.calls[1][: 3 + len(NEW)], ["tmux", "new-session", "-d", *NEW])
        self.assertEqual(fake.calls[1][-len(recorded("s")) :], recorded("s"))

    def test_a_new_session_tmux_does_not_name_is_killed(self):
        for out in ("", "4242 $3 %7\n", "4242 $3 %7 x\n"):
            with self.subTest(out=out):
                fake = FakeTmux(results={"new-session": (0, out, "")})
                with self.assertRaisesRegex(types.TuiError, "^tmux: bad new session "):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_start_runs_wrapper_decorates_dead_pane_without_opening_a_pane(
        self,
    ):
        # in tmux or iTerm2 all the same: start never shows the session
        for env in ({}, INSIDE, ITERM):
            with self.subTest(env=env), environ(**env), which():
                fake = FakeTmux()
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1],
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        *NEW,
                        "-s",
                        "s",
                        "-x",
                        "120",
                        "-y",
                        "40",
                        sys.executable,
                        "-I",
                        "-c",
                        EXEC,
                        fake.path,
                        fake.fifo,
                        ";",
                        "set-option",
                        "-p",
                        "-t",
                        "=s:",
                        "remain-on-exit",
                        "on",
                        *chained(decorations("s")),
                        *recorded("s"),
                    ],
                )
                self.assertNotIn("-c", fake.calls[1][: fake.calls[1].index(sys.executable)])
                self.assertEqual({a for a in fake.calls[1] if a.endswith(";")}, {";"})
                self.assertEqual(fake.kwargs, [TMUX_KW] * len(fake.calls))
                self.sleep.assert_not_called()
                self.assertEqual(self.stderr.getvalue(), "")

    def test_handover_file(self):
        # the env as given but its terminal keys; PWD the cwd, given another (as here) or none
        # (test_events_and_...)
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in TERMINAL_KEYS},
        }
        for argv in (("tool", "-x", "a b"), ("tool", "--settings", "s.json")):
            with self.subTest(argv=argv):
                fake = FakeTmux()
                self.start(fake, argv=argv, env=env)
                self.assertEqual(
                    fake._handover,
                    {
                        "argv": [self.tool, *argv[1:]],
                        "env": {
                            "PATH": self.bin,
                            "HOME": "/h",
                            "CHILD_SESSION": "1",
                            "PWD": self.cwd,
                        },
                        "cwd": self.cwd,
                    },
                )
                self.assertEqual(fake.mode, 0o600)
                self.assertEqual(fake.dir_mode, 0o700)
                self.assertEqual(os.path.dirname(os.path.dirname(fake.path)), self.temp)
                self.assertEqual(os.path.dirname(fake.fifo), os.path.dirname(fake.path))
                self.assertTrue(stat.S_ISFIFO(fake.fifo_mode))
                self.assertEqual(stat.S_IMODE(fake.fifo_mode), 0o600)
                self.assertEqual(os.listdir(self.temp), [])

    def test_terminal_keys(self):
        terminal = dict.fromkeys(
            (
                "TMUX",
                "TMUX_PANE",
                "TERM",
                "COLORTERM",
                "TERM_PROGRAM",
                "TERM_PROGRAM_VERSION",
                "TERM_SESSION_ID",
                "ITERM_SESSION_ID",
                "ITERM_PROFILE",
                "LC_TERMINAL",
                "LC_TERMINAL_VERSION",
                "COLUMNS",
                "LINES",
            ),
            "x",
        )
        fake = FakeTmux()
        self.start(fake, env={"PATH": self.bin, "HOME": "/h", "SHELL": "/bin/zsh", **terminal})
        self.assertEqual(
            fake._handover["env"],
            {"PATH": self.bin, "HOME": "/h", "SHELL": "/bin/zsh", "PWD": self.cwd},
        )

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = FakeTmux()
        self.start(fake)
        self.assertEqual(fake._handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])

    def test_launch_rejects_nul_before_tmux(self):
        fake = FakeTmux()
        with self.assertRaisesRegex(ConfigError, "launch: argv must be strings without NUL"):
            self.start(fake, argv=("tool", "a\0b"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_refused_before_tmux(self):
        # the command resolved with the env's PATH (no sh there); an events file that is no regular
        # file
        for kw, error in (
            ({"argv": ("sh", "-c", "true")}, "sh"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ()}, "no command"),
            ({"events": self.cwd}, ""),
        ):
            with self.subTest(**kw):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, error):
                    self.start(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = FakeTmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(types.TuiError, "^handover: "),
        ):
            self.start(fake)
        real = os.open

        def refuse(path, *args, **kw):
            if str(path).endswith("handover.json"):
                raise PermissionError(13, "Permission denied", path)
            return real(path, *args, **kw)

        with (
            unittest.mock.patch("os.open", side_effect=refuse),
            self.assertRaisesRegex(types.TuiError, "^handover: "),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(self.temp), [])

    def test_relative_command_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        for argv0, path in (("bin/tool", "/nonexistent"), ("tool", "bin")):
            fake = FakeTmux()
            self.start(fake, argv=(argv0,), env={"PATH": path})
            self.assertEqual(fake._handover["argv"], [self.tool])

    def test_live_session_is_refused(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env):
                fake = FakeTmux(pane="0  ")
                with self.assertRaisesRegex(
                    types.TuiError,
                    "^" + re.escape(f"session s is running; {prefix}tmux attach -t '=s'") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_dead_session_is_refused_untouched(self):
        for pane in ("1 0 ", "1  9"):
            with self.subTest(pane=pane):
                fake = FakeTmux(pane=pane)
                with self.assertRaisesRegex(types.TuiError, "^session s exists$"):
                    self.start(fake)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_unknown_session_is_never_killed(self):
        # tmux failing otherwise: new-session finds out; another start's session is not ours to kill
        for err in FAILED:
            with self.subTest(err=err):
                fake = FakeTmux(results={"display-message": (1, "", err)})
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
        fake = FakeTmux(
            results={
                "display-message": (1, "", "boom\n"),
                "new-session": (1, "", "duplicate session: s\n"),
            }
        )
        with self.assertRaisesRegex(types.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_tags_are_session_options_set_in_the_new_session_call(self):
        fake = FakeTmux()
        self.start(fake, tags={"@sid": "0123abcd-x", "@k": "a b#{x}"})
        self.assertEqual(
            fake.calls[1][fake.calls[1].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-p",
                "-t",
                "=s:",
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@sid",
                "0123abcd-x",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@k",
                "a b#{x}",
                *chained(decorations("s")),
                *recorded("s"),
            ],
        )
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_tags_refused_before_tmux(self):
        for tags in (
            {"sid": "x"},
            {"@": "x"},
            {"@a b": "x"},
            {"@a;": "x"},
            {"@k": "x;"},
            {"@k": 1},
            {"@k": None},
        ):
            with self.subTest(tags=tags):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, "^tag "):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_a_display_tag_tmux_could_not_list_or_a_nul_is_refused_before_tmux(self):
        for tags, error in (
            ({"@display": "a\tb"}, "^display "),
            ({"@display": "x;"}, "^display "),
            ({"@k": "a\0b"}, "^tag "),
        ):
            with self.subTest(tags=tags):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, error):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = FakeTmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
        self.assertEqual(os.listdir(self.temp), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_duplicate_session_is_not_killed(self):
        fake = FakeTmux(
            results={"new-session": (1, "", "duplicate session: s\n")}
        )  # another start took the name
        with self.assertRaisesRegex(types.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake = FakeTmux(hand_over=False)
        self.sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_tmux_missing(self):
        # status: unknown; new-session: the error
        def missing(_argv, **_kw):
            raise FileNotFoundError(2, "No such file or directory", "tmux")

        with self.assertRaisesRegex(types.TuiError, r"^tmux: "):
            self.start(missing)
        self.assertEqual(os.listdir(self.temp), [])

    def test_wrapper_never_reads(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = FakeTmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(types.TuiError, r"^the session did not start$"):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])
                self.assertEqual(self.stderr.getvalue(), "")

    def test_waits_for_a_late_wrapper(self):
        fake = FakeTmux(hand_over=False)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.unlink(fake.path)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_waits_for_a_late_exec(self):
        fake, slept = FakeTmux(wrapper="hang"), []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = FakeTmux(wrapper=errno.ENOEXEC)
        with self.assertRaisesRegex(
            types.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOEXEC)}") + "$",
        ):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_handover_at_its_last_poll_still_gets_the_whole_exec_wait(self):
        fake, slept = FakeTmux(hand_over=False), []
        last = round(HANDOVER_TIMEOUT / POLL)

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == last:
                fake.writer = os.open(fake.fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.unlink(fake.path)
            elif len(slept) == last + 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), last + 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_errpipe_data_is_an_error_and_kills_the_session(self):
        for data in (b"x", b"9" * 20):
            with self.subTest(data=data):
                fake = FakeTmux(wrapper=data)
                with self.assertRaisesRegex(
                    types.TuiError,
                    "^" + re.escape(f"cannot run {self.tool}: bad errpipe data") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_is_not_started(self):
        fake = FakeTmux(wrapper="hang")
        with self.assertRaisesRegex(types.TuiError, r"^the session did not start$"):
            self.start(fake)
        os.close(fake.writer)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertAlmostEqual(sum(c.args[0] for c in self.sleep.call_args_list), HANDOVER_TIMEOUT)
        self.assertEqual(os.listdir(self.temp), [])

    def test_unsafe_tmux_argument(self):
        for exe in ("/opt/py#3/bin/python3", "/opt/bin/python3;"):
            with self.subTest(exe=exe), unittest.mock.patch.object(sys, "executable", exe):
                fake = FakeTmux()
                with self.assertRaises(types.TuiError):
                    self.start(fake)
                self.assertNotIn("new-session", fake.commands())
        hashed = os.path.join(self.root, "t#mp")
        os.makedirs(hashed)
        fake = FakeTmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", hashed),
            self.assertRaises(types.TuiError),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(hashed), [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_invalid_session_name(self):
        for name in ("", "a b", "a.b", "a:b", "=a", "#{x}", "a;", "a\n", "é"):
            with self.subTest(name=name):
                fake = FakeTmux()
                for call in (
                    lambda *, fake=fake, name=name: self.start(fake, session=name),
                    lambda *, fake=fake, name=name: transport.status(name, proc=fake),
                    lambda *, fake=fake, name=name: Tmux(fake, self.sleep).detach(
                        name, Launch(("tool",), {"PATH": self.bin}, cwd=self.cwd)
                    ),
                    lambda *, fake=fake, name=name: transport.kill(name, proc=fake),
                    lambda *, fake=fake, name=name: transport.paste(name, "x", proc=fake),
                    lambda *, fake=fake, name=name: transport.keys(name, "Enter", proc=fake),
                    lambda *, fake=fake, name=name: transport.read(name, proc=fake),
                    lambda *, fake=fake, name=name: transport.option(name, "@sid", proc=fake),
                ):
                    with self.assertRaises(types.TuiError):
                        call()
                self.assertEqual(fake.calls, [])

    def test_events_and_decorations(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        events = os.path.join(self.root, "ev")
        # the status line is the caller's choice
        for given, path, status_line, status in (
            (None, None, False, "off"),
            ("ev", events, False, "off"),
            (None, None, True, "on"),
        ):
            with self.subTest(events=given, status_line=status_line):
                fake = FakeTmux()
                self.start(fake, env={"PATH": self.bin}, events=given, status_line=status_line)
                self.assertEqual(fake._handover["argv"], [self.tool, "-x", "a b"])
                self.assertEqual(fake._handover["env"], {"PATH": self.bin, "PWD": self.cwd})
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1][fake.calls[1].index(fake.fifo) + 8 :],
                    [*chained(decorations("s", path, status)), *recorded("s")],
                )
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)
