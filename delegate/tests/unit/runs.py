"""The runner's unit fakes: fake runtime steps (said, limit), session events (turn, STOP),
FakeHandle and no_child.
"""

import contextlib
import os
from unittest import mock

from agent_pm.delegate import Result
from agent_pm.session import Exited, TurnEnd

from session.tests.case import SPAWNS

NAME = "fake-11111111"
DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}
START = "[agent-pm-progress:start] go"
GAVE_UP = Result(0, None, "the agent run returned no outcome")


def said(*lines) -> dict:
    """A step printing a message record (Reply) of `lines`."""
    return {"out": {"type": "message", "text": "\n".join(lines)}}


def limit(resets_at) -> dict:
    return {"out": {"type": "limit", "resets_at": resets_at}}


@contextlib.contextmanager
def no_child():
    """Each of SPAWNS fails inside, and none was called."""
    with contextlib.ExitStack() as stack:
        spawns = [
            stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
            for target in SPAWNS
        ]
        yield
    for spawn in spawns:
        spawn.assert_not_called()


def turn(*lines, pending=0) -> TurnEnd:
    return TurnEnd("\n".join(lines), pending)


STOP = turn("Working.")


class FakeHandle:
    """agent_pm.session.Handle as the Delegate uses it: events() goes on through `events` across
    calls, up to an Exited (none left: AssertionError); send (raising `fail`) and stop (raising
    `stop_fail`) recorded in `calls`. start: the Delegate Driver's stand-in.
    """

    def __init__(self, events, fail=None, stop_fail=None):
        self.left, self.fail, self.stop_fail, self.calls = iter(events), fail, stop_fail, []
        self.exhausted = AssertionError("no event left")
        self.name, self.sid = NAME, None

    def start(self, spec, *, display=None, host=None, idle_timeout=None):
        spec = spec.session
        os.makedirs(spec.workdir, exist_ok=True)
        self.name, self.sid = spec.name or NAME, spec.sid
        return self

    def attach_command(self):
        return f"tmux attach -t '={self.name}'"

    def events(self):
        for event in self.left:
            yield event
            if isinstance(event, Exited):
                return
        raise self.exhausted

    def send(self, text):
        self.calls.append(("send", text))
        if self.fail:
            raise self.fail

    def stop(self):
        self.calls.append(("stop",))
        if self.stop_fail:
            raise self.stop_fail
