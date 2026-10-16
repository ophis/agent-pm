"""Session abstract interface and class methods."""

import inspect

from agent_pm.session import (
    Access,
    ConfigError,
    Hosts,
    Mode,
    Session,
    Spec,
)
from agent_pm.session.engine.types import ProcessOptions

from session.tests.case import Case
from session.tests.fakes.host import FakeHost
from session.tests.fakes.session import FakeSession

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
        "hide",
        "hidden",
        "attach_command",
        "screen",
        "press",
    }
}


class Abstract(Case):
    def test_each_interface_is_abstract_in_its_methods(self):
        for cls, methods in ABSTRACT.items():
            with self.subTest(cls=cls.__name__):
                self.assertTrue(inspect.isabstract(cls))
                self.assertEqual(cls.__abstractmethods__, methods)


class SessionClassMethods(Case):
    def test_policy_validation_needs_no_session_or_workdir(self):
        Session.validate_policy(flags=("--flag",), env={"TOKEN": "value", "A-B": "value"})
        for flags, env, message in (
            (("bad\0flag",), {}, "flags must be a tuple of strings without NUL"),
            ((), {"NOT=A=NAME": "value"}, "env must map variable names to strings without NUL"),
            ((), {"TOKEN": "bad\0value"}, "env must map variable names to strings without NUL"),
        ):
            with self.subTest(flags=flags, env=env):
                with self.assertRaises(ConfigError) as caught:
                    Session.validate_policy(flags=flags, env=env)
                self.assertEqual(str(caught.exception), message)
                with self.assertRaises(ConfigError) as spec_error:
                    Spec(workdir=self.home, access=Access(cwd=self.home), flags=flags, env=env)
                self.assertEqual(str(spec_error.exception), message)
                with self.assertRaises(ConfigError) as options_error:
                    ProcessOptions(flags=flags, env=env)
                self.assertEqual(str(options_error.exception), message)

    def test_public_config_boundary_rejects_missing_hosts_without_a_session(self):
        with self.assertRaises(ConfigError) as caught:
            Session.configure_from({})
        self.assertEqual(str(caught.exception), "[hosts]: missing")

    def test_on_a_subclass_each_is_its_own(self):
        self.assertIsInstance(FakeSession.host(), FakeHost)
        self.assertEqual(FakeSession.display("d"), "d")
        own = type("Own", (Session,), {})
        spec = Spec(
            workdir=self.home,
            access=Access(cwd=self.home),
            client="fake",
            mode=Mode.HEADLESS,
            prompt="hi",
        )
        calls = {
            "configure": ((), {"adapters": {}, "hosts": Hosts("tmux", {})}),
            "configure_from": (({},), {}),
            "start": ((spec,), {}),
            "attach": (("w1",), {}),
            "inspect": ((spec,), {}),
            "list": ((), {}),
            "usage": (("fake",), {}),
            "layout": ((), {}),
            "writable_dirs": ((spec,), {}),
            "display": ((), {}),
            "host": ((), {}),
            "class_roots": (("fake",), {}),
        }
        for name, (args, kwargs) in calls.items():
            with self.subTest(name=name), self.assertRaises(NotImplementedError) as caught:
                getattr(own, name)(*args, **kwargs)
            self.assertEqual(str(caught.exception), f"Own.{name}: not implemented")
