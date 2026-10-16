"""Session public exports."""

import inspect

from agent_pm import session
from agent_pm.session import (
    Access,
    Activity,
    Blocked,
    Effort,
    Exited,
    Hosts,
    IdleTimeout,
    KeyPress,
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
    StatusState,
    Stderr,
    TurnEnd,
    TurnFailed,
    Usage,
    Window,
)
from agent_pm.session.engine import types

from session.tests.case import Case

EVENTS = {Activity, Blocked, Exited, IdleTimeout, LimitHit, Reply, Stderr, TurnEnd, TurnFailed}

ALLOWED_CONCRETE = {
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
    KeyPress,
    Effort,
    Resumable,
    StatusState,
    RunLog,
    SessionInfo,
    Hosts,
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
