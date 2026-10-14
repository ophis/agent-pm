"""A caller-owned fake Session class (and its Host) for orchestrator unit tests."""

from agent_pm.session import (
    NoRecordedSession,
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
    """`live` None: the sessions unknown; `statuses` (per sid, default resumable yes), `usages`
    (per client, default LOW) and `shown`: a value or an exception to raise; `records`: the
    recorded sessions per sid, inspect's NoRecordedSession without one; detach raises
    `detach_error`, else its name goes live. host() is itself. `order` logs the close, show and
    detach calls.
    """

    def __init__(self, order=None):
        self.order = [] if order is None else order
        self.live, self.errors, self.shown, self.info = [], {}, None, []
        self.statuses, self.records, self.usages = {}, {}, {}
        self.usage_calls, self.detached, self.detach_error = [], [], None

    def host(self, name=None):
        return self

    def close(self, name):
        self.order.append(("close", name))
        if name in self.errors:
            raise self.errors[name]

    def show(self, name, *, display=None, command=None):
        self.order.append(("show", name, display, command))
        return _answer(self.shown)

    def inspect(self, spec):
        entry = self.records.get(spec.sid)
        self.info.append((spec.client, spec.sid, entry["cwd"] if entry else spec.workdir))
        if entry is None:
            raise NoRecordedSession(f"no recorded session {spec.sid} in {spec.workdir}")
        return _answer(
            self.statuses.get(spec.sid, SessionStatus("yes", None, f"resume {spec.sid}"))
        )

    def usage(self, client):
        self.usage_calls.append(client)
        return _answer(self.usages.get(client, LOW))

    def detach(self, name, launch, *, display=None, events=None, record=None):
        self.order.append(("detach", name))
        self.detached.append((name, list(launch.argv), launch.cwd, dict(launch.env)))
        if self.detach_error is not None:
            raise self.detach_error
        if self.live is not None:
            self.live.append(name)

    def attach_command(self, name):
        return f"attach {name}"
