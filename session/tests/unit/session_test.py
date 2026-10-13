"""Session public exports."""

import inspect

from agent_pm import session
from agent_pm.session import (
    Access,
    Activity,
    Blocked,
    Driver,
    Exited,
    Handle,
    IdleTimeout,
    LimitHit,
    Reply,
    SessionBound,
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
)

EVENTS = {Activity, Blocked, Exited, IdleTimeout, LimitHit, Reply, SessionBound, Stderr, TurnEnd}

ALLOWED_CONCRETE = {
    Driver,
    Handle,
    SessionStatusLog,
    Usage,
    Window,
    Spec,
    Access,
    SessionRow,
    SessionStatus,
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

    def test_the_only_concrete_classes_are_the_helpers_and_exceptions(self):
        concrete = {
            value
            for value in (getattr(session, n) for n in session.__all__)
            if inspect.isclass(value)
            and not issubclass(value, Exception)
            and not inspect.isabstract(value)
        }
        self.assertEqual(concrete, ALLOWED_CONCRETE)
