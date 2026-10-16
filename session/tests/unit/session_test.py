"""Session public exports."""

import inspect

from agent_pm import session
from agent_pm.session import (
    Access,
    Effort,
    Exited,
    IdleTimeout,
    Launch,
    LimitHit,
    Mode,
    Reply,
    Resumable,
    RunLog,
    SessionStatus,
    SessionStatusLog,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    Usage,
    Window,
)
from agent_pm.session.engine import types

from session.tests.case import Case

EVENTS = {Exited, IdleTimeout, LimitHit, Reply, Stderr, TurnEnd, TurnFailed}

ALLOWED_CONCRETE = {
    SessionStatusLog,
    Usage,
    Window,
    Spec,
    Access,
    SessionStatus,
    Mode,
    Launch,
    Effort,
    Resumable,
    RunLog,
    *EVENTS,
}


class Exports(Case):
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
