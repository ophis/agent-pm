"""A caller-owned fake Driver for orchestrator unit tests."""

import re

from agent_pm.session import (
    DriverError,
    SessionRow,
    SessionStatus,
    Usage,
    Window,
)

LOW = Usage(Window(10.0, None), Window(10.0, None))


def _answer(value):
    if isinstance(value, BaseException):
        raise value
    return value


class FakeApi:
    """`live` None: the sessions unknown (sessions raises DriverError); `statuses` (per sid, default
    resumable yes), `usages` (per runtime, default LOW) and `shown`: a value or an exception to
    raise; `records`: session_record's per sid; detach raises `detach_error`, else its name goes
    live. `order` logs the sessions, close, show and detach calls.
    """

    def __init__(self, order=None, gql=None):
        self.order = [] if order is None else order
        self.gql = gql
        self.live, self.errors, self.shown, self.info = [], {}, None, []
        self.statuses, self.records, self.usages = {}, {}, {}
        self.usage_calls, self.detached, self.detach_error = [], [], None

    def sessions(self, match=None):
        self.order.append(("sessions", match))
        if self.live is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        return [
            SessionRow(n, None, None, "working", None, None, None, None, own=False)
            for n in self.live
            if match is None or re.fullmatch(match, n)
        ]

    def close(self, name):
        self.order.append(("close", name))
        if name in self.errors:
            raise self.errors[name]

    def show(self, name, split=None, split_from=None, opener=None):
        calls = None if self.gql is None else len(self.gql().calls)
        self.order.append(("show", name, split, split_from, opener, calls))
        return _answer(self.shown)

    def session_status(self, runtime, sid, cwd, *, workdir):
        entry = self.session_record(workdir, sid)
        self.info.append((runtime, sid, entry["cwd"] if entry else cwd))
        if entry is None:
            return SessionStatus("no", None, "")
        return _answer(self.statuses.get(sid, SessionStatus("yes", None, f"resume {sid}")))

    def session_record(self, workdir, sid):
        return self.records.get(sid)

    def usage(self, runtime):
        self.usage_calls.append(runtime)
        return _answer(self.usages.get(runtime, LOW))

    def detach(self, name, argv, cwd, env, *, opener=None):
        self.order.append(("detach", name))
        self.detached.append((name, list(argv), cwd, dict(env)))
        if self.detach_error is not None:
            raise self.detach_error
        if self.live is not None:
            self.live.append(name)

    def attach_command(self, name):
        return f"attach {name}"
