"""Polling behaviour."""

import json
import os
import signal
import stat
from unittest import mock

from agent_pm.session.engine.types import (
    Activity,
    Blocked,
    LimitHit,
    SessionBound,
    TurnEnd,
)
from agent_pm.session.hosts.tmux import tmux_host

from session.tests.unit.tui.case import NAME, Base, record


class PollTest(Base):
    def test_records_become_the_runs_events_in_order_bound_once(self):
        mode = self.started()
        self.append(
            record("tool", session_id="n1", tool="Bash"),
            record("stop", session_id="n1", last_message="hi", tasks=[1, 2]),
        )
        self.assertEqual(
            mode.poll(0.5), ([SessionBound("n1"), Activity("Bash"), TurnEnd("hi", 2)], False)
        )
        self.append(
            record("ask", session_id="n2", why="perm"),
            record("other", session_id="n2"),
            record("tool"),
        )
        self.assertEqual(
            mode.poll(0.5), ([SessionBound("n2"), Blocked("perm"), Activity("")], False)
        )
        self.assertEqual(mode.bound, "n2")
        self.assertEqual(self.host.mock_calls, [mock.call.pane(NAME)] * 2)
        self.assertEqual(self.log, [])

    def test_a_partial_last_line_waits_for_its_end(self):
        mode = self.started()
        line = (json.dumps(record("stop", last_message="r")) + "\n").encode()
        self.append(record("tool"), line[:10])
        self.assertEqual(mode.poll(0), ([Activity("")], False))
        self.assertEqual(mode.poll(0), ([], False))
        self.append(line[10:])
        self.assertEqual(mode.poll(0), ([TurnEnd("r", 0)], False))

    def test_bad_lines_are_skipped(self):
        mode = self.started()
        self.append(
            b"not json\n",
            b"[1]\n",
            b'{"label": 1, "payload": {}}\n',
            b'{"label": "stop", "payload": []}\n',
            b'{"label": "stop"}\n',
            b"\xff\xfe\n",
            b"\n",
            b"[" * 100000 + b"\n",
            record("tool", tool="t"),
        )
        self.assertEqual(mode.poll(0), ([Activity("t")], False))

    def test_a_hooks_file_that_is_no_regular_file_gives_no_records(self):
        mode = self.started()
        real = os.path.join(self.tmp, "real.jsonl")
        with open(real, "w") as f:
            f.write(json.dumps(record("tool")) + "\n")
        for make in (lambda: os.symlink(real, self.hooks), lambda: os.mkfifo(self.hooks)):
            make()
            with self.subTest(stat.filemode(os.lstat(self.hooks).st_mode)):
                self.assertEqual(mode.poll(0), ([], False))
            os.unlink(self.hooks)

    def test_empty_poll_checks_status_and_pane_then_waits_in_slices(
        self,
    ):
        mode = self.started()
        self.assertEqual(mode.poll(0.5), ([], False))
        self.assertEqual(
            self.host.mock_calls,
            [mock.call.status(NAME), mock.call.read("%1"), mock.call.pane(NAME)],
        )
        self.assertTrue(all(0 < s <= 0.1 for _, s in self.log), self.log)
        self.assertAlmostEqual(self.clock.now, 0.5)

    def test_a_record_while_waiting_returns_at_once(self):
        mode = self.started()
        self.clock.actions.append((0.25, lambda: self.append(record("tool", tool="late"))))
        self.assertEqual(mode.poll(5), ([Activity("late")], False))
        self.assertAlmostEqual(self.clock.now, 0.3)

    def test_dead_or_absent_drains_the_records_then_ends(self):
        for status, code in ((3, 3), (-15, -15), (0, 0), (None, -signal.SIGHUP)):
            with self.subTest(status=status):
                mode = self.started()

                def ended(_name, status=status):
                    self.append(record("stop", last_message="last"))  # written before the pane died
                    return status

                self.host.status.side_effect = ended
                self.assertEqual(mode.poll(0.5), ([TurnEnd("last", 0)], True))
                self.host.read.assert_not_called()
                self.host.status.side_effect = AssertionError("status after the end")
                self.assertEqual(mode.returncode(1), code)
                self.host.status.side_effect = None

    def test_unknown_keeps_waiting_with_no_scan(self):
        mode = self.started()
        self.host.status.return_value = tmux_host.UNKNOWN
        self.assertEqual(mode.poll(0.2), ([], False))
        self.host.read.assert_not_called()

    def test_exact_events_per_poll_over_a_session(self):
        mode = self.started()
        self.host.read.side_effect = ["", "Sign in", "Sign in", "x", "LIMIT 9", "x"]
        script = [
            [record("tool", session_id="n1")],
            [],
            [],
            [record("ask", session_id="n1")],
            [],
            [],
            [record("stop", session_id="n1", last_message="done")],
        ]
        got = []
        for lines in script:
            self.append(*lines)
            got.append(mode.poll(0.1))
        self.assertEqual(
            got,
            [
                ([SessionBound("n1"), Activity("")], False),
                ([], False),
                ([Blocked("Sign in")], False),
                ([Blocked("")], False),
                ([], False),
                ([Activity("")], False),
                ([TurnEnd("done", 0)], False),
            ],
        )
        self.assertEqual(mode.poll(0.1), ([LimitHit(9.0)], False))
        self.assertEqual(mode.poll(0.1), ([], False))


class ScanTest(Base):
    def scans(self, mode, *screens) -> list:
        """mode.poll's events for each screen in turn."""
        found = []
        for screen in screens:
            self.host.read.return_value = screen
            found.append(mode.poll(0)[0])
        return found

    def test_a_dialog_is_blocked_once_and_its_end_activity(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "x", "a Trust this \n  folder? b", "Trust this folder?", "y", "y"),
            [[], [Blocked("Trust  this\nfolder?")], [], [Activity("")], []],
        )
        self.assertEqual(self.run.screens[1], "a Trust this folder? b")

    def test_two_dialogs_one_going_as_the_other_comes(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "Sign in; Trust this folder?", "Sign in", "Trust this folder?", ""),
            [
                [Blocked("Trust  this\nfolder?"), Blocked("Sign in")],
                [Activity("")],
                [Activity(""), Blocked("Trust  this\nfolder?")],
                [Activity("")],
            ],
        )

    def test_a_dialog_marks_the_pane_blocked_unless_it_is(self):
        mode = self.started()
        self.host.option.return_value = "done"
        self.scans(mode, "Sign in Trust this folder?")
        self.assertEqual(
            self.host.mock_calls[3:],
            [mock.call.option("%1", "@state"), mock.call.mark("blocked", self.events, pane="%1")],
        )
        self.host.reset_mock()
        self.host.option.return_value = "blocked"
        self.scans(mode, "", "Sign in")
        self.host.mark.assert_not_called()

    def test_a_name_with_no_pane_is_absent_unscanned(self):
        mode = self.started()
        self.host.pane.return_value = None
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))
        self.host.mark.assert_not_called()

    def test_a_failed_mark_still_yields_the_dialog(self):
        mode = self.started()
        for method in ("option", "mark"):
            with self.subTest(method):
                getattr(self.host, method).side_effect = tmux_host.TuiError("tmux: boom")
                self.assertEqual(
                    self.scans(mode, "Sign in", ""), [[Blocked("Sign in")], [Activity("")]]
                )
                getattr(self.host, method).side_effect = None

    def test_a_limit_is_one_event_per_appearance_its_reset_changing_or_not(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "LIMIT 100", "LIMIT  100", "LIMIT 200", "", "LIMIT 200", "LIMIT 300"),
            [[LimitHit(100.0)], [], [], [], [LimitHit(200.0)], []],
        )

    def test_a_failed_read_is_no_event(self):
        mode = self.started()
        self.host.read.side_effect = tmux_host.TuiError("tmux: gone")
        self.assertEqual(mode.poll(0), ([], False))


class ReturncodeStateTest(Base):
    def test_returncode_maps_the_status(self):
        for status, code in (
            (0, 0),
            (2, 2),
            (-9, -9),
            (None, -signal.SIGHUP),
            (tmux_host.RUNNING, None),
            (tmux_host.UNKNOWN, None),
        ):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value = status
                self.assertEqual(mode.returncode(0), code)

    def test_returncode_waits_its_timeout_in_slices_while_running(self):
        mode = self.started()
        self.clock.actions.append((0.25, lambda: setattr(self.host.status, "return_value", 4)))
        self.assertEqual(mode.returncode(5), 4)
        self.assertAlmostEqual(self.clock.now, 0.3)
        self.host.status.return_value = tmux_host.RUNNING
        mode = self.started()
        self.clock.now = 0
        self.assertIsNone(mode.returncode(0.35))
        self.assertAlmostEqual(self.clock.now, 0.35)

    def test_state_maps_the_status(self):
        for status, state in (
            (tmux_host.RUNNING, "live"),
            (0, "live"),
            (-15, "live"),
            (None, "absent"),
            (tmux_host.UNKNOWN, "unknown"),
        ):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value = status
                self.assertEqual(mode.state(), state)
