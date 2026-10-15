"""Domain events encode through the existing one-line DelegateEvent contract; each is delivered to
the callback argv from the run dir in its own process group, holding the callback lock that names its
pid; a failed delivery is one callback_error row.
"""

import fcntl
import json
import os
import signal
from dataclasses import asdict
from unittest import mock

from agent_pm.delegate import (
    Begun,
    DelegateEvent,
    Finished,
    Limit,
    Outcome,
    OutcomeStatus,
    Progress,
    Result,
)
from agent_pm.delegate.engine.callback import CALLBACK_TIMEOUT, EventCallback
from agent_pm.delegate.engine.state import RunState
from agent_pm.delegate.engine.types import Layout
from agent_pm.utils import Stopped

from delegate.tests.case import Case
from delegate.tests.harness import SID
from utils.tests.process_fixtures import DEVNULL, PIPE, TimeoutExpired

RUN = {"sid": SID, "attempt": 3}
ARGV = ("writeback", "--issue", "A B")
PID = 4242


class Codec(Case):
    def test_each_actual_domain_event_preserves_its_fields_in_one_line_stamped_with_its_run(self):
        outcome = Outcome(OutcomeStatus.DONE, "Title", "Summary", deliverable="# Body\n")
        events = (
            (Begun(SID), DelegateEvent("begun", **RUN)),
            (
                Progress("draft", "one\n二"),
                DelegateEvent("progress", **RUN, name="draft", text="one\n二"),
            ),
            (
                Finished(Result(7, outcome, "exit diagnostic")),
                DelegateEvent(
                    "finished",
                    **RUN,
                    returncode=7,
                    outcome=asdict(outcome),
                    error="exit diagnostic",
                ),
            ),
            (
                Finished(Result(0, None, "usage limit", Limit(1767225600.0))),
                DelegateEvent(
                    "finished",
                    **RUN,
                    returncode=0,
                    error="usage limit",
                    limit={"resets_at": 1767225600.0},
                ),
            ),
        )
        callback = EventCallback(("record",), RunState(Layout("/unused")), **RUN)
        for event, expected in events:
            with self.subTest(event=event):
                encoded = callback.encode(event)
                self.assertEqual(encoded, expected)
                line = encoded.to_line()
                self.assertEqual(line.splitlines(), [line])
                self.assertEqual(DelegateEvent.parse(line), expected)


class Child:
    """subprocess.Popen as EventCallback uses it, scripted by `Child.outcome`: an int is its exit
    code, an exception is raised by communicate. Records each spawn and call in `Child.calls`.
    """

    outcome: int | BaseException = 0
    calls: list = []

    def __init__(self, argv, **kwargs):
        Child.calls.append(("spawn", argv, kwargs))
        self.pid, self.returncode = PID, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        Child.calls.append(("exit",))

    def communicate(self, input=None, timeout=None):
        Child.calls.append(("communicate", input, timeout))
        if isinstance(Child.outcome, BaseException):
            raise Child.outcome
        self.returncode = Child.outcome
        return None, None

    def wait(self, timeout=None):
        Child.calls.append(("wait",))
        self.returncode = -signal.SIGKILL
        return self.returncode

    def kill(self):
        Child.calls.append(("kill",))


def store(workdir: str) -> RunState:
    """A RunState whose run dir exists."""
    layout = Layout(workdir)
    os.makedirs(layout.run)
    return RunState(layout)


def failing_first_write(error: BaseException):
    """os.write raising `error` once: the pid write, the first write of a delivery."""
    write, raised = os.write, []

    def first(descriptor, data):
        if not raised:
            raised.append(error)
            raise error
        return write(descriptor, data)

    return mock.patch("os.write", side_effect=first)


class Delivery(Case):
    def setUp(self):
        self.store = store(os.path.join(self.home, "w"))
        self.work = self.store.layout.run
        Child.outcome, Child.calls = 0, []
        self.popen = self.enterContext(mock.patch("subprocess.Popen", side_effect=Child))
        self.killpg = self.enterContext(
            mock.patch(
                "os.killpg",
                side_effect=lambda pid, signum: Child.calls.append(("killpg", pid, signum)),
            )
        )
        self.callback = EventCallback(ARGV, self.store, **RUN)

    def errors(self, work=None) -> list[dict]:
        try:
            with open(os.path.join(work or self.work, "run.jsonl"), encoding="utf-8") as file:
                rows = [json.loads(line) for line in file]
        except FileNotFoundError:
            return []
        return [row for row in rows if row["kind"] == "callback_error"]

    def test_the_timeout_is_a_300_second_module_constant(self):
        self.assertEqual(CALLBACK_TIMEOUT, 300)

    def lock_text(self) -> str:
        with open(self.store.callback_lock, encoding="utf-8") as file:
            return file.read()

    def test_one_line_on_stdin_of_the_argv_from_the_run_dir_in_its_own_group(self):
        event = Progress("draft", "one\n二")
        self.assertIsNone(self.callback(event))
        (spawn, communicate, _exit) = Child.calls
        _, argv, kwargs = spawn
        self.assertEqual(tuple(argv), ARGV)
        self.assertEqual(
            (kwargs["cwd"], kwargs["stdin"], kwargs["stdout"], kwargs["process_group"]),
            (self.work, PIPE, DEVNULL, 0),
        )
        self.assertNotIn("shell", kwargs)
        _, given, timeout = communicate
        self.assertEqual(timeout, CALLBACK_TIMEOUT)
        self.assertEqual(given, self.callback.encode(event).to_line() + "\n")
        self.assertEqual(given.count("\n"), 1)
        self.assertEqual(DelegateEvent.parse(given).text, "one\n二")
        self.assertEqual(self.errors(), [])

    def test_a_failed_delivery_is_one_callback_error_row_without_a_retry(self):
        for index, (outcome, ending, error) in enumerate(
            (
                (7, ["communicate", "exit"], "callback exited 7"),
                (
                    TimeoutExpired(list(ARGV), CALLBACK_TIMEOUT),
                    ["communicate", "killpg", "wait", "exit"],
                    f"callback timed out after {CALLBACK_TIMEOUT} s",
                ),
            )
        ):
            with self.subTest(outcome=outcome):
                failed = store(os.path.join(self.home, f"failed-{index}"))
                work = failed.layout.run
                Child.outcome, Child.calls = outcome, []
                callback = EventCallback(ARGV, failed, **RUN)
                self.assertIsNone(callback(Begun(SID)))
                self.assertEqual([call[0] for call in Child.calls], ["spawn", *ending])
                if "killpg" in ending:
                    self.assertEqual(Child.calls[2], ("killpg", PID, signal.SIGKILL))
                self.assertEqual([row["error"] for row in self.errors(work)], [error])

    def test_a_command_that_cannot_start_is_one_callback_error_row(self):
        self.popen.side_effect = FileNotFoundError(2, "No such file or directory", ARGV[0])
        self.assertIsNone(self.callback(Begun(SID)))
        self.assertEqual(self.popen.call_count, 1)
        (row,) = self.errors()
        self.assertIn("No such file or directory", row["error"])

    def test_an_exception_while_waiting_kills_and_reaps_the_group_then_propagates(self):
        stopped = Stopped(143)
        Child.outcome = stopped
        with self.assertRaises(Stopped) as raised:
            self.callback(Begun(SID))
        self.assertIs(raised.exception, stopped)
        self.assertEqual(
            [call[0] for call in Child.calls], ["spawn", "communicate", "killpg", "wait", "exit"]
        )
        self.assertEqual(Child.calls[2], ("killpg", PID, signal.SIGKILL))
        self.assertEqual(self.errors(), [])

    def test_the_child_inherits_the_held_callback_lock_naming_its_pid(self):
        seen = []

        def spawned(argv, **kwargs):
            seen.append((len(kwargs["pass_fds"]), self.store.held(self.store.callback_lock)))
            seen.append(self.lock_text())
            return Child(argv, **kwargs)

        self.popen.side_effect = spawned
        self.callback(Begun(SID))
        self.assertEqual(seen, [(1, True), ""])
        self.assertEqual(self.lock_text(), f"{PID}\n")
        self.assertFalse(self.store.held(self.store.callback_lock))

    def test_a_held_callback_lock_spawns_nothing_and_is_one_callback_error_row(self):
        holder = os.open(self.store.callback_lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        self.assertIsNone(self.callback(Begun(SID)))
        self.assertEqual(Child.calls, [])
        self.assertEqual(
            [row["error"] for row in self.errors()],
            [f"callback lock {self.store.callback_lock} held: an earlier callback still runs"],
        )

    def test_a_signal_while_naming_the_pid_kills_and_reaps_the_group_then_propagates(self):
        stopped = Stopped(143)
        with failing_first_write(stopped), self.assertRaises(Stopped) as raised:
            self.callback(Begun(SID))
        self.assertIs(raised.exception, stopped)
        self.assertEqual([call[0] for call in Child.calls], ["spawn", "killpg", "wait", "exit"])
        self.assertFalse(self.store.held(self.store.callback_lock))

    def test_a_failed_pid_write_kills_the_group_and_is_one_callback_error_row(self):
        failure = OSError(28, "No space left on device")
        with failing_first_write(failure):
            self.assertIsNone(self.callback(Begun(SID)))
        self.assertEqual([call[0] for call in Child.calls], ["spawn", "killpg", "wait", "exit"])
        self.assertEqual(
            [row["error"] for row in self.errors()],
            [f"callback lock {self.store.callback_lock}: [Errno 28] No space left on device"],
        )
