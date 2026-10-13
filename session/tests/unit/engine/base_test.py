"""Session abstract interface and class methods."""

import inspect

from agent_pm.session import (
    Session,
    Spec,
)

from session.tests.case import Case

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
    }
}


class Abstract(Case):
    def test_each_interface_is_abstract_in_its_methods(self):
        for cls, methods in ABSTRACT.items():
            with self.subTest(cls=cls.__name__):
                self.assertTrue(inspect.isabstract(cls))
                self.assertEqual(cls.__abstractmethods__, methods)


class SessionClassMethods(Case):
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
