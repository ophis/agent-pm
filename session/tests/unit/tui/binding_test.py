"""Binding behaviour."""

import os
import signal
from unittest import mock

from agent_pm.session.engine.types import (
    Blocked,
    DriverError,
)
from agent_pm.session.hosts.tmux import tmux_host

from session.tests.unit.tui.case import NAME, Base, record


class MissingPane(tmux_host.TuiError):
    def __init__(self, target):
        super().__init__(f"tmux: can't find pane: {target}")


class BoundTest(Base):
    """A begun Mode is its pane's (%1): another pane under its name, or stop(), ends it."""

    def test_another_pane_under_the_name_ends_it_at_once_unscanned_unmarked_and_unread(self):
        for status in (tmux_host.RUNNING, 3):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value, self.host.pane.return_value = status, "%2"
                self.host.read.return_value = "Sign in LIMIT 9"
                self.log.clear()
                self.assertEqual(mode.poll(5), ([], True))
                self.assertEqual(
                    (self.run.screens, self.log, mode.returncode(0)), ([], [], -signal.SIGHUP)
                )
                self.host.mark.assert_not_called()
                self.append(record("tool"))
                self.host.reset_mock()
                self.assertEqual(mode.poll(5), ([], True))
                self.assertEqual(self.host.mock_calls, [])

    def test_records_under_another_pane_are_never_read(self):
        mode = self.started()
        self.host.pane.return_value = "%2"
        self.append(record("stop", last_message="theirs"))
        self.assertEqual(mode.poll(5), ([], True))
        mode = self.started()

        def theirs():
            self.host.pane.return_value = "%2"
            self.append(record("stop", last_message="theirs"))

        self.clock.actions.append((self.clock.now + 0.25, theirs))
        self.assertEqual((mode.poll(5), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_stop_ends_it_at_once_its_code_the_one_seen_else_sighup(self):
        mode = self.started()
        mode.stop()
        os.mkdir(self.dir)
        self.append(record("tool"))
        self.host.reset_mock()
        self.assertEqual((mode.poll(5), mode.returncode(5)), (([], True), -signal.SIGHUP))
        self.assertEqual((self.host.mock_calls, self.log), ([], []))
        mode = self.started()
        self.host.status.return_value = 3
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.assertEqual(mode.returncode(0), 3)

    def test_stop_kills_its_pane_while_it_holds_the_name_the_name_when_tmux_fails(self):
        for holder, kills, removed in (
            ("%1", [mock.call("%1")], True),
            ("%2", [], False),
            (tmux_host.TuiError("tmux: boom"), [mock.call(NAME)], True),
        ):
            with self.subTest(holder=holder):
                mode = self.started()
                self.host.pane.return_value = holder
                self.host.pane.side_effect = holder if isinstance(holder, Exception) else None
                mode.stop()
                self.assertEqual(
                    (self.host.kill.call_args_list, os.path.isdir(self.dir)), (kills, not removed)
                )
                self.assertEqual(mode.poll(0), ([], True))
                self.host.pane.side_effect = None

    def test_stop_with_the_name_absent_kills_nothing_as_a_new_server_may_reuse_its_pane_id(self):
        mode = self.started()
        self.host.pane.return_value = None
        self.host.status.side_effect = lambda target: tmux_host.RUNNING if target == "%1" else None
        mode.stop()
        self.assertEqual(self.host.mock_calls, [mock.call.pane(NAME)])
        self.assertNoSessionDir()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_stop_checks_the_holder_and_closes_under_the_names_lock(self):
        held = []
        for holder, closed in (("%1", True), ("%2", False)):
            with self.subTest(holder=holder):
                self.host.pane.side_effect = self.host.kill.side_effect = None
                mode = self.started()
                held.clear()
                self.host.pane.side_effect = lambda _name, *, holder=holder: (
                    held.append(self.locked()) or holder
                )
                self.host.kill.side_effect = lambda _name: held.append(self.locked())
                mode.stop()
                self.assertEqual((held, self.locked()), ([True] * (1 + closed), False))

    def test_another_pane_under_the_name_is_absent_to_state_send_and_screen(self):
        mode = self.started()
        self.host.pane.return_value = "%2"
        self.assertEqual(mode.state(), "absent")
        for call in (lambda: mode.send("hi"), mode.screen):
            with self.assertRaisesRegex(DriverError, r"\Atui session w1 is not running\Z"):
                call()
        self.host.paste.assert_not_called()
        self.host.read.assert_not_called()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_with_no_pane_bound_it_reads_no_screen_and_stops_killing_nothing(self):
        self.host.pane.return_value = None  # begin found no pane
        mode = self.prepare()
        mode.begin(("/bin/run",), cwd=self.tmp, env={})
        self.host.status.return_value = tmux_host.RUNNING
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.host.read.assert_not_called()
        self.host.kill.assert_not_called()
        self.assertNoSessionDir()

    def test_its_pane_unknown_is_no_scan_and_no_end(self):
        mode = self.started()
        self.host.pane.side_effect = tmux_host.TuiError("tmux: boom")
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(
            (self.run.screens, mode.returncode(0), mode.state()), ([], None, "unknown")
        )


class SwapTest(Base):
    """A new session takes the name right after the Mode's pane check: every later call targets its
    pane (%1), so none reaches the new pane (%2).
    """

    def swapped(self, screen: str = "") -> list:
        """After started(): the next pane(NAME) answers %1, then %2 holds the name and %1 is gone
        (absent to status and option, a TuiError to the rest). Returns (call, pane) for each call
        that reached a pane.
        """
        holder, reached = ["%1"], []

        def at(target):
            return holder[0] if target in (NAME, holder[0]) else None

        def check(_name):
            found, holder[0] = holder[0], "%2"
            return found

        def act(method, result):
            def call(target, *_args):
                pane = at(target)
                if pane is None and method != "option":
                    raise MissingPane(target)
                if pane is not None:
                    reached.append((method, pane))
                return result

            return call

        self.host.pane.side_effect = check
        self.host.status.side_effect = lambda target: tmux_host.RUNNING if at(target) else None
        for method, result in (
            ("paste", None),
            ("keys", None),
            ("read", screen),
            ("option", None),
            ("kill", None),
        ):
            getattr(self.host, method).side_effect = act(method, result)
        return reached

    def error(self, call) -> str | None:
        try:
            call()
        except DriverError as e:
            return str(e)
        return None

    def test_send_never_types_into_the_new_pane(self):
        mode = self.started()
        reached = self.swapped()
        self.assertEqual(
            (self.error(lambda: mode.send("hi")), reached),
            ("send to tui session w1: tmux: can't find pane: %1", []),
        )

    def test_screen_never_reads_the_new_pane(self):
        mode = self.started()
        reached = self.swapped()
        self.assertEqual(
            (self.error(mode.screen), reached),
            ("screen of tui session w1: tmux: can't find pane: %1", []),
        )

    def test_poll_reads_and_marks_by_its_pane_only(self):
        mode = self.started()
        reached = self.swapped("Sign in")
        self.assertEqual((mode.poll(0), mode.poll(0)), (([Blocked("Sign in")], False), ([], True)))
        self.assertEqual(reached, [("read", "%1")])
        self.host.mark.assert_called_once_with("blocked", self.events, pane="%1")

    def test_stop_never_kills_the_new_session(self):
        mode = self.started()
        reached = self.swapped()
        mode.stop()
        self.assertEqual((reached, os.path.isdir(self.dir)), ([], False))
        self.host.kill.assert_called_once_with("%1")
