"""hosts/tmux/host.py: TmuxHost over an autospecced tmux_host module (no tmux runs here): the display
format (fold, unfold, display, display_help), detach's forwarding and refusals, attach, list, the
Displayable forwards and TmuxProcess (its pane and process group; os.killpg faked).
"""

import json
import logging
import os
import re
import signal
import socket
import tempfile
from pathlib import Path
from unittest import mock

from agent_pm.session import ConfigError, Displayable, DriverError, Host
from agent_pm.session.hosts.base import SIGNALS
from agent_pm.session.hosts.tmux import TmuxHost, tmux_host
from agent_pm.session.hosts.tmux.host import TmuxProcess
from agent_pm.utils import ProcessControl, Record, load_class

from session.tests.case import Case

TuiError = tmux_host.TuiError
NO_PANE = "no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)"
LINE = Record(1, "role", "r1", ("r1",), None, None, ("ctl",), ("ctl", "resume"), None, None)
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
PROCESS = "4242 $3 %7 4343"


def fake_tmux() -> mock.Mock:
    """tmux_host autospecced, its constants, attach_command and events_file real: status RUNNING,
    listing [], opener "mgr", option None, detach and process PROCESS.
    """
    tmux = mock.create_autospec(tmux_host)
    for name in ("PaneStatus", "TuiError"):
        setattr(tmux, name, getattr(tmux_host, name))
    for name in ("attach_command", "events_file"):
        getattr(tmux, name).side_effect = getattr(tmux_host, name)
    tmux.status.return_value = tmux_host.PaneStatus.RUNNING
    tmux.listing.return_value = []
    tmux.opener.return_value = "mgr"
    tmux.option.return_value = None
    tmux.detach.return_value = tmux.process.return_value = PROCESS
    return tmux


def row(name: str, display: str | None = None) -> dict:
    return {"name": name, "display": display}


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.tmux = fake_tmux()
        self.host = TmuxHost("tmux", {"workers_per_column": 2}, tmux=self.tmux)
        self.killpg = self.enterContext(
            mock.patch("os.killpg", side_effect=AssertionError("unsafe signal"))
        )
        self.control = ProcessControl.prepare(["tool", "a b"], self.dir)
        self.addCleanup(self.control.remove)
        control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        control_socket.bind(os.path.join(self.control.path, "control"))
        self.addCleanup(control_socket.close)
        self.completed(self.control.path)
        self.controlled = True
        self.kind = "detach"
        self.tmux.option.side_effect = lambda _pane, key: (
            self.control.path
            if key == "@control" and self.controlled
            else self.kind
            if key == "@kind"
            else self.tmux.option.return_value
        )
        self.tmux.detach.side_effect = self.detached
        self.socket = self.enterContext(mock.patch("socket.socket"))
        self.connection = self.socket.return_value.__enter__.return_value
        self.connection.makefile.return_value.__enter__.return_value.readline.return_value = (
            b'{"live": true}\n'
        )

    @staticmethod
    def completed(path):
        for name, message in (("started.json", {"started": True}), ("done.json", {"rc": 0})):
            Path(path, name).write_text(json.dumps(message))

    def detached(self, *_args, **kw):
        self.control = ProcessControl(kw["control"])
        self.addCleanup(self.control.remove)
        Path(self.control.path, "started.json").write_text('{"started": true}')
        return PROCESS

    def active(self, *responses):
        self.controlled = True
        Path(self.control.path, "done.json").unlink(missing_ok=True)
        stream = self.connection.makefile.return_value.__enter__.return_value
        stream.readline.side_effect = [
            json.dumps(response).encode() + b"\n" for response in responses
        ]


class ClassTest(Case):
    def test_a_displayable_host_loaded_by_its_config_name(self):
        cls = load_class("agent_pm.session.hosts.tmux:TmuxHost", Host)
        self.assertIs(cls, TmuxHost)
        self.assertTrue(issubclass(cls, Displayable))
        host = TmuxHost("tmux", {"status_line": True})
        self.assertEqual(
            (host.name, host.settings, host.tmux), ("tmux", {"status_line": True}, tmux_host)
        )

    def test_terminal_comes_with_task_368(self):
        with self.assertRaisesRegex(NotImplementedError, "^TmuxHost.terminal comes with TASK-368$"):
            TmuxHost("tmux", tmux=fake_tmux()).terminal("t1", ["x"], "/", {})


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
    def detach(self, **kw):
        return self.host.detach("d1", ["tool", "a b"], self.dir, {"PATH": "/bin"}, **kw)

    def test_forwards_with_the_signals_blocked_the_opener_part_and_the_record_line(self):
        events = os.path.join(self.dir, "events")
        process = self.detach(display="mgr/below", events=events, record=LINE)
        self.tmux.detach.assert_called_once_with(
            "d1",
            self.control.command(),
            cwd=self.dir,
            env={"PATH": "/bin"},
            block=SIGNALS,
            opener="mgr",
            display="mgr/below",
            events=events,
            record=LINE.to_line(),
            control=self.control.path,
        )
        self.assertEqual((process.name, process.alive()), ("d1", True))
        self.assertEqual(
            json.loads(Path(self.control.path, "launch.json").read_text()),
            {"argv": ["tool", "a b"], "cwd": self.dir},
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


class AttachListTest(Base):
    def test_a_tui_session_without_a_controller_keeps_pane_based_handling(self):
        self.kind, self.controlled = None, False
        process = self.host.attach("tui")
        self.assertTrue(process.alive())
        process.close()
        self.tmux.kill.assert_called_once_with("%7")
        self.socket.assert_not_called()

    def test_a_pre_upgrade_session_is_refused_immediately(self):
        self.controlled = False
        with self.assertRaisesRegex(DriverError, "tmux session d1: missing @control"):
            self.host.attach("d1")
        self.socket.assert_not_called()
        self.tmux.kill.assert_not_called()

    def test_a_pane_cannot_redirect_control_to_an_arbitrary_directory_or_socket(self):
        self.controlled = False
        sentinel = Path(self.dir, "keep")
        sentinel.write_text("owned data")
        for path in (self.dir, "/tmp", os.path.join(self.dir, "control")):
            with self.subTest(path=path):
                self.tmux.option.return_value = path
                with self.assertRaisesRegex(DriverError, "tmux session d1: invalid @control"):
                    self.host.attach("d1")
                self.assertEqual(sentinel.read_text(), "owned data")
        self.socket.assert_not_called()
        self.tmux.kill.assert_not_called()

    def test_a_controller_directory_with_public_permissions_is_refused(self):
        self.controlled = True
        os.chmod(self.control.path, 0o755)
        with self.assertRaisesRegex(DriverError, "invalid @control"):
            self.host.attach("d1")
        self.socket.assert_not_called()

    def test_a_regular_file_in_place_of_the_controller_socket_is_refused(self):
        self.controlled = True
        Path(self.control.path, "control").unlink()
        Path(self.control.path, "control").write_text("not a socket")
        with self.assertRaisesRegex(DriverError, "invalid @control"):
            self.host.attach("d1")
        self.socket.assert_not_called()

    def test_attach_a_live_session_else_a_driver_error(self):
        self.assertEqual(self.host.attach("d1").name, "d1")
        for name, state, message in (
            ("d1", None, "no tmux session d1"),
            ("d1", tmux_host.PaneStatus.UNKNOWN, "tmux session d1: tmux failed"),
            ("a b", tmux_host.PaneStatus.RUNNING, "no tmux session 'a b'"),
        ):
            with self.subTest(name=name, state=state):
                self.tmux.status.return_value = state
                with self.assertRaises(DriverError) as caught:
                    self.host.attach(name)
                self.assertEqual(str(caught.exception), message)
        self.tmux.status.return_value = tmux_host.PaneStatus.RUNNING
        for found, message in (
            (None, "no tmux session d1"),
            (TuiError("tmux: boom"), "tmux session d1: tmux: boom"),
        ):
            with self.subTest(found=found):
                self.tmux.process.side_effect = [found]
                with self.assertRaises(DriverError) as caught:
                    self.host.attach("d1")
                self.assertEqual(str(caught.exception), message)

    def test_list_every_name_or_those_whose_display_equals(self):
        self.tmux.listing.return_value = [
            row("a", "mgr"),
            row("b", None),
            row("c", "elsewhere:1.2"),
            row("d", "mgr"),
        ]
        for display, want in (
            (None, ["a", "b", "c", "d"]),
            ("mgr", ["a", "d"]),
            ("elsewhere:1.2", ["c"]),
            ("other", []),
        ):
            with self.subTest(display=display):
                self.assertEqual(self.host.list(display), want)
        self.tmux.listing.reset_mock()
        self.assertEqual(self.host.list(""), [])
        self.tmux.listing.assert_not_called()

    def test_list_when_tmux_fails_is_a_driver_error(self):
        self.tmux.listing.return_value = None
        with self.assertRaisesRegex(DriverError, "^tmux sessions unknown: tmux failed$"):
            self.host.list()


class ProcessTest(Base):
    def test_record_is_the_stored_line_parsed_else_none(self):
        process = self.host.attach("d1")
        for stored, want in (
            (LINE.to_line(), LINE),
            (None, None),
            ("", None),
            ("{forged", None),
            ('{"v":1}', None),
            (LINE.to_line().replace('"ctl"]', '"/bin/sh"]', 1), None),
        ):
            with self.subTest(stored=stored):
                self.tmux.option.return_value = stored
                self.assertEqual(process.record(), want)
        self.tmux.option.assert_called_with("%7", "@record")

    def test_display_is_the_stored_value_verbatim(self):
        process = self.host.attach("d1")
        for stored in (None, "mgr/right", "elsewhere:1.2"):
            with self.subTest(stored=stored):
                self.tmux.option.return_value = stored
                self.assertEqual(process.display(), stored)
        self.tmux.option.assert_called_with("%7", "@display")

    def test_alive_while_running_wait_exit_until_not(self):
        self.active({"live": False})
        process = self.host.attach("d1")
        self.tmux.status.side_effect = [
            tmux_host.PaneStatus.RUNNING,
            tmux_host.PaneStatus.RUNNING,
            0,
            None,
        ]
        self.assertTrue(process.alive())
        self.assertTrue(process.wait_exit(5))
        self.assertFalse(process.alive())
        self.tmux.status.side_effect = None
        self.tmux.status.return_value = tmux_host.PaneStatus.RUNNING
        process = self.host.attach("d1")
        self.assertFalse(process.wait_exit(0))
        self.tmux.status.return_value = tmux_host.PaneStatus.UNKNOWN
        with self.assertRaisesRegex(DriverError, "^tmux session d1: tmux failed$"):
            process.alive()

    def test_close_kills_its_group_then_its_panes_session_absent_is_fine(self):
        self.active({"live": False})
        process = self.host.attach("d1")
        process.close()
        self.connection.sendall.assert_called_once_with(b"kill\n")
        self.killpg.assert_not_called()
        self.tmux.kill.assert_called_once_with("%7")
        self.tmux.kill.side_effect = TuiError("tmux: can't find pane: %7")
        self.tmux.process.side_effect = [PROCESS, None]
        process.close()
        self.tmux.process.side_effect = None
        with self.assertRaisesRegex(DriverError, "^close d1: tmux: can't find pane: %7$"):
            process.close()

    def test_after_its_pane_ends_wait_exit_waits_for_its_group_and_kill_ends_it(self):
        self.active({"live": True}, {"live": False})
        process = self.host.attach("d1")
        self.tmux.status.return_value = None
        self.assertFalse(process.alive())
        self.assertFalse(process.wait_exit(0))
        process.kill()
        self.assertEqual(
            self.connection.sendall.call_args_list, [mock.call(b"live\n"), mock.call(b"kill\n")]
        )
        self.connection.sendall.reset_mock()
        self.assertTrue(process.wait_exit(0))
        process.kill()
        process.close()
        self.connection.sendall.assert_not_called()
        self.killpg.assert_not_called()

    def test_unobserved_exit_and_retained_dead_pane_never_signal_a_reused_group(self):
        self.controlled = True
        old = self.host.attach("d1")
        self.tmux.status.return_value = 0
        attached_dead = self.host.attach("d1")
        for process in (old, attached_dead):
            process.kill()
            process.close()
            self.assertTrue(process.wait_exit(0))
        self.socket.assert_not_called()
        self.killpg.assert_not_called()

    def test_permission_denied_retains_control_for_later_cleanup(self):
        self.active(
            {"error": "permission denied", "errno": 1},
            {"error": "permission denied", "errno": 1},
            {"live": False},
        )
        process = self.host.attach("d1")
        self.tmux.status.return_value = 0
        for operation in (lambda: process.wait_exit(0), process.kill):
            with self.assertRaisesRegex(DriverError, "permission denied"):
                operation()
        process.kill()
        self.assertTrue(process.wait_exit(0))
        self.assertEqual(self.connection.sendall.call_count, 3)

    def test_a_handle_never_acts_on_a_later_pane_of_its_name(self):
        process = self.host.attach("d1")
        for later in (None, "4242 $4 %9 5555", "9999 $3 %7 4343", "4242 $3 %7 5555"):
            with self.subTest(later=later):
                self.tmux.process.return_value = later
                self.tmux.reset_mock()
                self.tmux.process.return_value = later
                self.assertEqual(
                    (process.alive(), process.record(), process.display()), (False, None, None)
                )
                process.close()
                self.tmux.status.assert_not_called()
                self.tmux.option.assert_not_called()
                self.tmux.kill.assert_not_called()
                self.tmux.process.assert_called_with("%7")
        self.killpg.assert_not_called()

    def test_a_tmux_failure_reading_metadata_is_a_driver_error(self):
        process = self.host.attach("d1")
        self.tmux.option.side_effect = TuiError("tmux: boom")
        with self.assertRaisesRegex(DriverError, "^tmux session d1: tmux: boom$"):
            process.record()
        self.tmux.process.side_effect = TuiError("tmux: down")
        for call in (process.alive, process.display, process.close):
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(DriverError, "^tmux session d1: tmux: down$"),
            ):
                call()


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
        self.tmux.status.return_value = tmux_host.PaneStatus.RUNNING
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
        self.assertIs(TmuxProcess("w1", PROCESS, tmux=self.tmux).tmux, self.tmux)

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
