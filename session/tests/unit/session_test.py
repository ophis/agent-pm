"""Session public exports."""

import inspect

from agent_pm import session
from agent_pm.session import (
    Access,
    Activity,
    ActivityState,
    Blocked,
    Driver,
    Effort,
    Exited,
    Hosts,
    IdleTimeout,
    Launch,
    LimitHit,
    Liveness,
    Mode,
    Reply,
    Resumable,
    RunLog,
    SessionInfo,
    SessionRow,
    SessionStatus,
    SessionStatusLog,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.engine import types

from session.tests.case import Case

NAMES = (
    "Session",
    "Host",
    "HostProcess",
    "Terminal",
    "Displayable",
    "NotDisplayable",
    "SessionStatusLog",
    "Usage",
    "DriverError",
    "ConfigError",
    "Spec",
    "Access",
    "RunLog",
    "SessionInfo",
    "SessionEvent",
    "Hosts",
    "Launch",
)

EVENTS = {Activity, Blocked, Exited, IdleTimeout, LimitHit, Reply, Stderr, TurnEnd}

ALLOWED_CONCRETE = {
    Driver,
    SessionStatusLog,
    Usage,
    Window,
    Spec,
    Access,
    SessionRow,
    SessionStatus,
    Mode,
    Liveness,
    Launch,
    Effort,
    Resumable,
    ActivityState,
    RunLog,
    SessionInfo,
    Hosts,
    *EVENTS,
}


class Exports(Case):
    def test_all_holds_the_new_names_each_bound(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assertIn(name, session.__all__)
        self.assertEqual([n for n in session.__all__ if not hasattr(session, n)], [])

    def test_the_types_are_engine_types(self):
        for name in session.__all__:
            if hasattr(types, name):
                with self.subTest(name=name):
                    self.assertIs(getattr(session, name), getattr(types, name))

    def test_no_native_binding_crosses_the_session_boundary(self):
        for name in ("SessionBound", "ClientBound"):
            with self.subTest(name=name):
                self.assertNotIn(name, session.__all__)

    def test_the_only_concrete_classes_are_the_helpers_and_exceptions(self):
        concrete = {
            value
            for value in (getattr(session, n) for n in session.__all__)
            if inspect.isclass(value)
            and not issubclass(value, Exception)
            and not inspect.isabstract(value)
        }
        self.assertEqual(concrete, ALLOWED_CONCRETE)
