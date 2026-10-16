"""TmuxHost launch/display boundaries and process-close ordering over recorded calls.
Attachment trust uses real metadata in integration/hosts/tmux/process_integration_test.py.
"""

import json
import logging
import os
import re
import signal
import time
from pathlib import Path
from unittest import mock

from agent_pm.session import ConfigError, Host, InteractiveProcess, Launch, SessionError, Showable
from agent_pm.session.engine.types import Key, KeyPress, Paste, SessionRow
from agent_pm.session.hosts.base import SIGNALS
from agent_pm.session.hosts.tmux import TmuxHost, transport, types
from agent_pm.session.hosts.tmux.host import TmuxProcess, TmuxTerminal
from agent_pm.session.hosts.tmux.transport import (
    Tmux,
)
from agent_pm.utils import ProcessControl, StatusState, load_class

from session.tests.case import Case
from session.tests.hosted_support import SID, listed
from session.tests.tmux_fixtures import (
    INSIDE,
    SESSIONS,
    SOCK,
    FakeTmux,
    TileFixture,
    environ,
    hold,
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
    def test_a_showable_host_loaded_by_its_config_name(self):
        cls = load_class("agent_pm.session.hosts.tmux:TmuxHost", Host)
        self.assertIs(cls, TmuxHost)
        self.assertTrue(issubclass(cls, Showable))
        host = TmuxHost("tmux", {"status_line": True})
        self.assertEqual((host.name, host.settings), ("tmux", {"status_line": True}))
        self.assertIsInstance(host.tmux, Tmux)
        self.assertEqual(
            (f"{host.tmux.runner.__module__}.{host.tmux.runner.__name__}", host.tmux.sleep),
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
    def test_display_help_names_the_format(self):
        help_text = self.host.display_help()
        self.assertIn("<opener>[/<split>[/<split_from>]]", help_text)
        self.assertIn("right or below", help_text)

    def test_the_callers_opener_fills_an_empty_or_absent_opener_part(self):
        self.runner.pane = "mgr"
        self.enterContext(environ(**INSIDE))
        for value, want in (
            (None, "mgr"),
            ("", "mgr"),
            ("/right", "mgr/right"),
            ("//m1", "mgr//m1"),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.host.display(value), want)

    def test_a_given_opener_or_a_foreign_value_is_returned_as_given(self):
        self.enterContext(environ(**INSIDE))
        for value in ("w1", "w1/below/m1", *FOREIGN):
            with self.subTest(value=value):
                self.assertEqual(self.host.display(value), value)
        self.assertEqual(self.runner.calls, [])

    def test_no_opener_available_returns_the_value_unchanged(self):
        self.enterContext(environ())
        for value in (None, "", "/right"):
            with self.subTest(value=value):
                self.assertEqual(self.host.display(value), value)

    def test_a_display_tmux_could_not_list_is_a_config_error(self):
        self.enterContext(environ(**INSIDE))
        for value in ("mgr\tx", "mgr\nx", "mgr;", 5):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                self.host.display(value)
            self.assertEqual(str(caught.exception), transport.display_error(value))
        self.assertEqual(self.runner.calls, [])


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
        events, real_open = os.path.join(self.dir, "events"), os.open

        def refused(path, *args, **kw):
            if path == events:
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kw)

        with (
            mock.patch("os.open", side_effect=refused),
            self.assertRaisesRegex(SessionError, f"events file {events}: Permission denied"),
        ):
            self.detach(events=events)
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
        self.assertIsInstance(terminal, InteractiveProcess)
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


class ShowableTest(Base):
    def test_show_opens_a_pane_beside_split_from_on_the_displays_side(self):
        self.enterContext(environ())
        self.runner.results.update(
            {
                "list-clients": (0, f"5 /dev/ttys004 %9 0 {SOCK}\n"),
                "pgrep": (1,),
                "split-window": (0, "%10\n"),
            }
        )
        self.assertIsNone(self.host.show("w1", display="mgr/below/m1"))
        split = next(c for c in self.runner.calls if c[1] == "split-window")
        self.assertEqual(split[3], "-v")
        self.assertEqual(split[split.index("-t") + 1], "%9")
        self.assertEqual(split[-3:], ["attach", "-t", "=w1"])

    def test_show_without_a_pane_logs_why_and_returns_the_attach_command(self):
        self.enterContext(environ())
        why = f"{NO_PANE}; watch it with tmux attach -t '=w1'"
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(self.host.show("w1"), "tmux attach -t '=w1'")
        self.assertEqual(logs.output, [f"WARNING:session:show w1: {why}"])
        self.assertEqual(self.runner.calls, [])

    def test_show_with_a_command_runs_it_instead_of_opening_a_pane(self):
        sh = ["/bin/sh", "-c"]
        self.assertIsNone(self.host.show("w1", command="open -a T {{session}}"))
        self.assertEqual(self.runner.calls, [[*sh, "open -a T w1"]])
        self.runner.results["/bin/sh"] = (3,)
        with self.assertLogs("session", logging.WARNING) as logs:
            self.assertEqual(self.host.show("w1", command="false"), "tmux attach -t '=w1'")
        self.assertEqual(logs.output, ["WARNING:session:show w1: exit status 3"])
        del self.runner.results["/bin/sh"]
        self.assertIsNone(self.host.show("w1", display="elsewhere:1.2", command="true"))
        self.assertEqual(self.runner.calls, [[*sh, "open -a T w1"], [*sh, "false"], [*sh, "true"]])

    def test_show_refuses_an_absent_session_a_foreign_display_or_a_bad_setting(self):
        self.tmux.status.return_value = None
        with self.assertRaisesRegex(SessionError, "^no tmux session w1$"):
            self.host.show("w1")
        self.tmux.status.return_value = types.PaneStatus.RUNNING
        with self.assertRaisesRegex(ConfigError, r"^display 'elsewhere:1\.2': not <opener>"):
            self.host.show("w1", display="elsewhere:1.2")
        for key, value, want in (
            ("workers_per_column", 0, "per_column must be an int from 1 to 9999, not 0"),
            ("grid_retile", "x", "retile must be one of all, open-close, off, not 'x'"),
        ):
            with self.subTest(key=key), self.assertRaises(ConfigError) as caught:
                self.host.settings[key] = value
                self.host.show("w1")
            self.host.settings = {"workers_per_column": 2}
            self.assertEqual(str(caught.exception), want)
        self.assertEqual(self.runner.calls, [])

    def test_layout_completes_the_display_once_the_pane_is_found(self):
        self.enterContext(environ(**INSIDE))
        self.runner.pane = "mgr"
        self.runner.results["list-clients"] = (0, f"5 /dev/ttys004 %9 0 {SOCK}\n")
        for display, want in (
            (None, "mgr"),
            ("/right", "mgr/right"),
            ("w1/below", "w1/below"),
            ("/below/m1", "mgr/below/m1"),
        ):
            with self.subTest(display=display):
                self.assertEqual(self.host.layout(display=display), want)
        self.runner.calls.clear()
        self.assertEqual(self.host.layout(display="w1/below"), "w1/below")
        self.assertEqual(self.runner.calls, [])

    def test_layout_beside_split_from_without_an_opener_keeps_the_part_empty(self):
        self.enterContext(environ())
        self.runner.results["list-clients"] = (0, f"5 /dev/ttys004 %9 0 {SOCK}\n")
        self.assertEqual(self.host.layout(display="//m1"), "//m1")

    def test_layout_without_a_pane_or_split_from_is_refused(self):
        self.enterContext(environ())
        hint = ": run from tmux or iTerm2, or pass split_from"
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


class HideTest(Base):
    def options(self, **found):
        recorded = self.tmux.option.side_effect
        self.tmux.option.return_value = None
        self.tmux.option.side_effect = lambda pane, key: found.get(key[1:], recorded(pane, key))

    def test_hide_kills_the_shown_pane_and_marks_it_hidden_until_a_show(self):
        self.options(pane="%9", sid=SID)
        self.assertIsNone(self.host.hide("w1"))
        self.tmux.kill.assert_called_once_with("%9")
        self.tmux.set_option.assert_called_once_with("%7", "@hidden", "1")
        self.options(hidden="1")
        self.assertTrue(self.host.attach("w1").status().hidden)
        self.options(sid=SID)
        self.host.show("w1", command="true")
        self.tmux.set_option.assert_called_with("%7", "@hidden", "0")
        self.options(hidden="0")
        self.assertFalse(self.host.attach("w1").hidden())

    def test_hide_leaves_an_iterm2_or_closed_pane_and_still_marks_it_hidden(self):
        for pane, process in (("w0t0p0:ABC", PROCESS), ("%9", None), (None, PROCESS)):
            with self.subTest(pane=pane, process=process):
                self.tmux.reset_mock()
                self.options(pane=pane, sid=SID)
                self.tmux.process.side_effect = lambda target, found=process: (
                    found if target == "%9" else PROCESS
                )
                self.host.hide("w1")
                self.tmux.kill.assert_not_called()
                self.tmux.set_option.assert_called_once_with("%7", "@hidden", "1")

    def test_hide_kills_the_pane_of_a_sidless_session_and_marks_nothing(self):
        self.options(pane="%9")
        self.tmux.process.return_value = PROCESS
        self.tmux.process.side_effect = None
        self.host.hide("w1")
        self.tmux.kill.assert_called_once_with("%9")
        self.tmux.set_option.assert_not_called()

    def test_hide_refuses_an_absent_session(self):
        self.tmux.status.return_value = None
        with self.assertRaisesRegex(SessionError, "^no tmux session w1$"):
            self.host.hide("w1")
        self.tmux.kill.assert_not_called()
        self.tmux.set_option.assert_not_called()

    def test_hide_names_a_failed_pane_kill_and_marks_nothing(self):
        self.options(pane="%9", sid=SID)
        self.tmux.kill.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
            self.host.hide("w1")
        self.tmux.set_option.assert_not_called()

    def test_hide_names_a_failed_hidden_mark(self):
        self.options(pane="%9", sid=SID)
        self.tmux.set_option.side_effect = TuiError("tmux: lost server")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: lost server$"):
            self.host.hide("w1")
        self.tmux.kill.assert_called_once_with("%9")

    def test_a_failed_show_clears_hidden_while_a_pane_it_opened_still_shows_it(self):
        self.enterContext(environ())
        self.options(pane="%9", sid=SID)
        self.assertIsNotNone(self.host.show("w1"))
        self.tmux.set_option.assert_called_once_with("%7", "@hidden", "0")
        self.tmux.set_option.reset_mock()
        self.options(pane=None, sid=SID)
        self.host.show("w1")
        self.tmux.set_option.assert_not_called()


class ScreenTest(Base):
    def test_screen_reads_the_launched_panes_last_lines(self):
        self.tmux.read.return_value = "text"
        self.assertEqual(self.host.screen("w1", 5), "text")
        self.tmux.read.assert_called_once_with("%7", 5)

    def test_screen_of_a_name_without_a_launch_reads_its_active_pane(self):
        self.tmux.read.return_value = "text"
        self.recorded = None
        self.assertEqual(self.host.screen("w1"), "text")
        self.tmux.read.assert_called_once_with("w1", None)

    def test_screen_names_a_failed_read(self):
        self.tmux.read.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: boom$"):
            self.host.screen("w1")

    def test_screen_refuses_a_bad_launch_record_before_reading(self):
        self.recorded = "4242 $3 %x 4343"
        with self.assertRaisesRegex(
            SessionError, "^tmux session w1: bad @process '4242 \\$3 %x 4343'$"
        ):
            self.host.screen("w1")
        self.tmux.read.assert_not_called()

    def test_screen_names_a_failed_launch_record_read_before_reading(self):
        self.tmux.option.side_effect = TuiError("tmux: lost server")
        with self.assertRaisesRegex(SessionError, "^tmux session w1: tmux: lost server$"):
            self.host.screen("w1")
        self.tmux.read.assert_not_called()

    def test_screen_refuses_a_bad_name_before_tmux(self):
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.host.screen("a b")
        self.tmux.option.assert_not_called()
        self.tmux.read.assert_not_called()


class AttachCommandTest(Base):
    def test_attach_command_names_the_session_and_refuses_a_bad_name(self):
        self.assertEqual(self.host.attach_command("w1"), "tmux attach -t '=w1'")
        with self.assertRaisesRegex(SessionError, "^no tmux session 'a b'$"):
            self.host.attach_command("a b")


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
        self.enterContext(environ(**INSIDE))
        self.runner.pane = "mgr"
        self.tmux.listing.return_value = [
            listed("mgr"),
            listed("w1", sid=SID, client="fake", state="done", display="mgr/below", shown="m"),
            listed("w2", sid="a/b", client="Fake", state="blocked", shown=""),
            listed("w3", client="x-y", state="dead"),
            listed("w4", client="fake", state="done", dead=True, last_lines="bye"),
            listed("w5", client="fake9_", state="other", dead=None),
        ]
        rows = self.host.rows()
        self.assertEqual([type(r.state) for r in rows], [StatusState] * 6)
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


class TileTest(TileFixture):
    """TmuxHost.tile over a fake runner (TileFixture's window @3)."""

    def tile(self, fake, *args, **kw):
        TmuxHost("tmux", tmux=Tmux(fake)).tile(*args, **kw)
        return fake

    def test_tile_lays_out_the_window_unless_its_pane_set_is_unchanged(self):
        fake = self.tile(self.fake(*self.WIDE), "@3")
        self.assertEqual(
            fake.calls,
            [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
        )
        fake = self.tile(self.fake(*self.WIDE), "@3", if_panes_changed=True)
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, self.UNMUTE])

    def test_tile_without_wait_returns_while_another_tile_holds_the_lock(self):
        hold(self, self.lock)
        fake = self.tile(self.fake(*self.WIDE), "@3", wait=False)
        self.assertEqual(fake.calls, [self.SOCKET])

    def test_a_failed_tile_is_a_session_error(self):
        with self.assertRaisesRegex(SessionError, "^tile @3: tmux: boom$"):
            self.tile(FakeTmux(fail=("display-message",)), "@3")


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
