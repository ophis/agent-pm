"""delegate/engine/base.py: the Delegate ABC, no constructor, the Spec and delegate/__init__.py's
new exports.
"""

import dataclasses
import inspect
import unittest

from agent_pm import delegate, session
from agent_pm.delegate import Delegate, composer, event
from agent_pm.session import Host, Session

from delegate.tests.case import Case

SPEC = {
    "input": None,
    "workdir": None,
    "task": None,
    "mode": "headless",
    "client": None,
    "sid": None,
    "resume": False,
    "name": None,
    "events": None,
    "out": None,
    "read": (),
    "cwd": None,
    "team": None,
}
ABSTRACT = {
    "attach",
    "usage",
    "roles",
    "resolve",
    "trusted_dirs",
    "check_tui",
    "detach",
    "run",
    "alive",
    "status",
    "outcome",
    "show",
    "attach_command",
    "close",
    "forget",
    "session_info",
    "resume",
}
CLASS_METHODS = {"attach", "usage", "roles", "resolve", "trusted_dirs", "check_tui"}


class Exports(Case):
    def test_all_holds_the_new_names_each_bound(self):
        for name in ("Delegate", "Spec", "RunConfig", "DelegateEvent", "Driver", "DriverSpec"):
            with self.subTest(name=name):
                self.assertIn(name, delegate.__all__)
        self.assertEqual([n for n in delegate.__all__ if not hasattr(delegate, n)], [])
        self.assertIs(delegate.RunConfig, composer.RunConfig)
        self.assertIs(delegate.DelegateEvent, event.DelegateEvent)

    def test_no_concrete_implementation_is_exported(self):
        exported = [getattr(module, n) for module in (session, delegate) for n in module.__all__]
        concrete = [
            value
            for value in exported
            if inspect.isclass(value)
            and issubclass(value, (Session, Host, Delegate))
            and not inspect.isabstract(value)
        ]
        self.assertEqual(concrete, [])


class Abstract(Case):
    def test_delegate_is_abstract_in_every_member(self):
        self.assertTrue(inspect.isabstract(Delegate))
        self.assertEqual(Delegate.__abstractmethods__, ABSTRACT)
        for name in CLASS_METHODS:
            with self.subTest(name=name):
                self.assertIsInstance(inspect.getattr_static(Delegate, name), classmethod)

    def test_no_constructor_is_declared(self):
        self.assertNotIn("__init__", vars(Delegate))


class SpecTest(Case):
    def test_a_frozen_spec_its_fields_and_defaults(self):
        found = {
            f.name: None if f.default is dataclasses.MISSING else f.default
            for f in dataclasses.fields(delegate.Spec)
        }
        self.assertEqual(found, SPEC)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            delegate.Spec("x", "/w").sid = "s"


if __name__ == "__main__":
    unittest.main()
