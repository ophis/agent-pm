"""session/__init__.py's interfaces: the exported names, which are abstract, NotDisplayable, the bare Session's class
methods and a Host's defaults."""

import inspect
import unittest

from agent_pm import session
from agent_pm.session import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    Displayable,
    Driver,
    DriverError,
    Exited,
    Handle,
    Host,
    HostProcess,
    IdleTimeout,
    LimitHit,
    NotDisplayable,
    Reply,
    Session,
    SessionBound,
    SessionRow,
    SessionStatus,
    SessionStatusLog,
    Spec,
    Stderr,
    Terminal,
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
PROCESS = {"alive", "close", "wait_exit", "kill", "record", "display"}
ABSTRACT = {
    Session: {
        "events",
        "send",
        "state",
        "stop",
        "info",
        "reply",
        "close",
        "session_status",
        "record",
        "displayable",
        "show",
        "attach_command",
        "screen",
    },
    Host: {"detach", "terminal", "attach", "list"},
    HostProcess: PROCESS,
    Terminal: PROCESS | {"send"},
    Displayable: {"show", "attach_command", "screen", "layout", "tile"},
}


class MinimalHost(Host):
    def detach(self, *args, **kwargs):
        raise AssertionError("not called")

    terminal = attach = list = detach


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


class Abstract(Case):
    def test_each_interface_is_abstract_in_its_methods(self):
        for cls, methods in ABSTRACT.items():
            with self.subTest(cls=cls.__name__):
                self.assertTrue(inspect.isabstract(cls))
                self.assertEqual(cls.__abstractmethods__, methods)

    def test_a_terminal_is_a_host_process(self):
        self.assertTrue(issubclass(Terminal, HostProcess))

    def test_not_displayable_is_a_driver_error(self):
        self.assertTrue(issubclass(NotDisplayable, DriverError))
        self.assertEqual(
            str(NotDisplayable("host fake cannot display")), "host fake cannot display"
        )


class SessionClassMethods(Case):
    def test_configure_is_concrete_and_refuses_bad_input(self):
        with self.assertRaisesRegex(ConfigError, r"\Ahosts: want a mapping"):
            self.driver = Driver(adapters={}, hosts=1, default_host="fake")

    def test_each_but_configure_raises_on_the_bare_abc_naming_task_365(self):
        spec = Spec(workdir=self.home)
        calls = {
            "start": (("fake", "headless", "hi", spec), {"resume": None, "display": None}),
            "attach": (("w1",), {"idle_timeout": None}),
            "list": ((), {"display": None}),
            "usage": (("fake",), {}),
            "layout": ((), {}),
            "writable_dirs": (("fake", spec), {}),
            "display": ((), {"host": None}),
            "host": ((), {}),
        }
        for name, (args, kwargs) in calls.items():
            with self.subTest(name=name), self.assertRaises(NotImplementedError) as caught:
                getattr(Session, name)(*args, **kwargs)
            self.assertEqual(
                str(caught.exception),
                f"Session.{name}: the session layer's own implementation comes with HostedSession"
                " (TASK-365)",
            )


class HostDefaults(Case):
    def test_name_and_a_copy_of_settings(self):
        settings = {"root": "/x"}
        host = MinimalHost("m", settings)
        self.assertEqual((host.name, host.settings), ("m", settings))
        self.assertIsNot(host.settings, settings)
        self.assertEqual(MinimalHost("m").settings, {})

    def test_display_returns_its_value_and_display_help_is_empty(self):
        host = MinimalHost("m")
        self.assertFalse(isinstance(host, Displayable))
        self.assertEqual(host.display("elsewhere:1.2"), "elsewhere:1.2")
        self.assertIsNone(host.display(None))
        self.assertEqual(host.display_help(), "")


if __name__ == "__main__":
    unittest.main()
