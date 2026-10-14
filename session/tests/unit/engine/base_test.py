"""Session abstract interface and class methods."""

import inspect

from agent_pm.session import (
    Hosts,
    Mode,
    Session,
    Spec,
)

from session.tests.case import Case
from session.tests.fakes.host import FakeHost
from session.tests.fakes.session import FakeSession

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

    def test_no_config_parser_hangs_on_session(self):
        self.assertFalse(hasattr(Session, "parse_hosts"))


class SessionClassMethods(Case):
    def test_start_and_attach_take_the_idle_timeout_writable_dirs_only_the_spec(self):
        key, either = inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD
        for method, parameters in (
            (Session.configure, [("adapters", key), ("hosts", key)]),
            (
                Session.start,
                [("spec", either), ("display", key), ("host", key), ("idle_timeout", key)],
            ),
            (Session.attach, [("name", either), ("host", key), ("idle_timeout", key)]),
            (Session.writable_dirs, [("spec", either)]),
            (Session.layout, [("display", key)]),
            (Session.show, [("self", either), ("display", key)]),
        ):
            with self.subTest(method=method.__name__):
                found = inspect.signature(method).parameters.values()
                self.assertEqual([(p.name, p.kind) for p in found], parameters)

    def test_on_a_subclass_each_is_its_own(self):
        self.assertIsInstance(FakeSession.host(), FakeHost)
        self.assertEqual(FakeSession.display("d"), "d")
        own = type("Own", (Session,), {})
        spec = Spec(workdir=self.home, runtime="fake", mode=Mode.HEADLESS, prompt="hi")
        calls = {
            "configure": ((), {"adapters": {}, "hosts": Hosts("tmux", {})}),
            "start": ((spec,), {}),
            "attach": (("w1",), {}),
            "list": ((), {}),
            "usage": (("fake",), {}),
            "layout": ((), {}),
            "writable_dirs": ((spec,), {}),
            "display": ((), {}),
            "host": ((), {}),
        }
        for name, (args, kwargs) in calls.items():
            with self.subTest(name=name), self.assertRaises(NotImplementedError) as caught:
                getattr(own, name)(*args, **kwargs)
            self.assertEqual(str(caught.exception), f"Own.{name}: not implemented")
