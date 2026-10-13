"""Attach behaviour."""

import json
import signal
from unittest import mock

from agent_pm.session.engine.types import (
    Activity,
    Blocked,
    DriverError,
    SessionBound,
    TurnEnd,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.tui import types as tui

from session.tests.unit.tui.case import NAME, SID, Base, record


class AttachTest(Base):
    def setUp(self):
        super().setUp()
        self.host.option.side_effect = lambda _name, key: {"@sid": SID}.get(key)

    def attach(self, *lines) -> tui.TuiMode:
        """TuiMode.attach on a prepared session dir whose hooks.jsonl has lines, the host's calls
        cleared first.
        """
        self.prepare()
        self.append(*lines)
        self.host.reset_mock()
        return tui.TuiMode.attach(NAME, tui.read_state(NAME), self.run, host=self.host)

    def test_reply_bound_and_the_next_record_from_the_files_end(self):
        partial = (json.dumps(record("tool", tool="next")) + "\n").encode()
        mode = self.attach(
            record("tool", session_id="n1"),
            record("stop", session_id="n1", last_message="r1"),
            record("stop", session_id="n2", last_message="r2"),
            record("ask"),
            b"bad\n",
            partial[:7],
        )
        self.assertEqual(
            (mode.name, mode.sid, mode.events, mode.reply, mode.bound),
            (NAME, SID, self.events, "r2", "n2"),
        )
        self.assertEqual(
            self.host.mock_calls, [mock.call.pane(NAME), mock.call.option("%1", "@sid")]
        )
        self.append(partial[7:], record("stop", session_id="n2", last_message="r3"))
        self.assertEqual(mode.poll(0), ([Activity("next"), TurnEnd("r3", 0)], False))

    def test_no_records_yet(self):
        mode = self.attach()
        self.assertEqual((mode.reply, mode.bound), (None, ""))
        self.append(record("stop", session_id="n1"))
        self.assertEqual(mode.poll(0), ([SessionBound("n1"), TurnEnd("", 0)], False))

    def test_a_reply_of_the_last_turn_end_even_when_empty(self):
        mode = self.attach(record("stop", last_message="r1"), record("stop"), record("tool"))
        self.assertEqual(mode.reply, "")

    def test_blocked_by_its_last_record_of_a_kind_else_by_its_panes_state(self):
        self.host.pane.return_value = "%7"
        partial = json.dumps(record("tool")).encode()
        for lines, state, blocked in (
            ((record("tool"), record("ask"), record("other"), b"bad\n", partial), None, True),
            ((record("ask"), record("tool")), None, False),
            ((record("ask"), record("stop")), "done", False),
            ((record("stop"),), "blocked", True),
            ((), "blocked", True),
            ((), "working", False),
            ((), tmux_host.TuiError("tmux: denied"), False),
        ):
            with self.subTest(lines=lines, state=state):

                def option(target, key, *, state=state):
                    if (target, key) == ("%7", "@state") and isinstance(state, Exception):
                        raise state
                    return {("%7", "@sid"): SID, ("%7", "@state"): state}.get((target, key))

                self.host.option.side_effect = option
                self.assertIs(self.attach(*lines).blocked, blocked)

    def test_another_sid_absent_or_unknown_is_no_tui_session(self):
        for found in ("other", None, tmux_host.TuiError("tmux: denied")):
            with self.subTest(found=found):
                self.host.option.side_effect = (
                    found
                    if isinstance(found, Exception)
                    else lambda _name, _key, *, found=found: found
                )
                with self.assertRaises(DriverError) as caught:
                    self.attach()
                self.assertEqual(str(caught.exception), "no tui session w1")
        self.host.option.side_effect = lambda _name, key: {"@sid": SID}.get(key)
        for pane in (None, tmux_host.TuiError("tmux: denied")):
            with self.subTest(pane=pane):
                self.host.pane.return_value = pane
                self.host.pane.side_effect = pane if isinstance(pane, Exception) else None
                with self.assertRaisesRegex(DriverError, r"\Ano tui session w1\Z"):
                    self.attach()
                self.host.option.assert_not_called()

    def test_it_reads_the_sid_of_the_pane_it_took_so_a_pane_swapped_in_is_never_bound(self):
        live = {"%7": SID}  # pane -> its session's @sid; the one pane holds the name

        def then_swap(answer):
            def call(*args):
                found = answer(*args)
                if self.host.pane.call_count + self.host.option.call_count == 1:
                    live.clear()
                    live["%8"] = SID  # a new session under the name
                return found

            return call

        self.host.pane.side_effect = then_swap(lambda _name: next(iter(live)))
        self.host.option.side_effect = then_swap(
            lambda target, _key: live.get(next(iter(live)) if target == NAME else target)
        )
        try:
            bound = self.attach()._pane
        except DriverError as e:
            bound = str(e)
        self.assertEqual(bound, "no tui session w1")
        self.assertEqual(
            self.host.mock_calls, [mock.call.pane(NAME), mock.call.option("%7", "@sid")]
        )

    def test_an_attached_session_is_live_and_marks_dialogs_on_its_events_file(self):
        self.host.pane.return_value = "%7"
        mode = self.attach()
        self.host.status.return_value = tmux_host.RUNNING
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([Blocked("Sign in")], False))
        self.host.mark.assert_called_once_with("blocked", self.events, pane="%7")
        mode.stop()
        self.host.kill.assert_called_once_with("%7")

    def test_an_attached_session_ends_once_another_pane_holds_its_name(self):
        self.host.pane.return_value = "%7"
        mode = self.attach()
        self.host.status.return_value, self.host.pane.return_value = tmux_host.RUNNING, "%8"
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))
