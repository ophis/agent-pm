"""delegate/engine/base.py: the Delegate ABC, its constructor convention and delegate/__init__.py's new exports."""

import inspect
import unittest

from agent_pm import delegate, session
from agent_pm.delegate import Delegate, composer, event
from agent_pm.session import Host, Session

from delegate.tests.case import Case

POSITIONAL = ["role", "task", "input", "workdir"]
KEYWORD = {
    "runtime": None,
    "mode": "headless",
    "driver": None,
    "sid": None,
    "name": None,
    "repo": None,
    "cwd": None,
    "team": None,
    "out": None,
    "events": None,
    "callback": None,
    "display": None,
    "host": None,
    "harness_host": None,
    "session_cls": None,
    "composer": None,
}
ABSTRACT = {
    "__init__",
    "attach",
    "usage",
    "roles",
    "resolve",
    "trusted_dirs",
    "detach",
    "run",
    "alive",
    "status",
    "outcome",
    "show",
    "attach_command",
    "close",
    "session_info",
    "resume",
}
CLASS_METHODS = {"attach", "usage", "roles", "resolve", "trusted_dirs"}


class Exports(Case):
    def test_all_holds_the_new_names_each_bound(self):
        for name in ("Delegate", "RunConfig", "DelegateEvent"):
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

    def test_the_constructor_convention(self):
        params = list(inspect.signature(Delegate.__init__).parameters.values())[1:]
        self.assertEqual([p.name for p in params if p.kind is p.POSITIONAL_OR_KEYWORD], POSITIONAL)
        self.assertEqual({p.name: p.default for p in params if p.kind is p.KEYWORD_ONLY}, KEYWORD)
        self.assertEqual(len(params), len(POSITIONAL) + len(KEYWORD))


if __name__ == "__main__":
    unittest.main()
