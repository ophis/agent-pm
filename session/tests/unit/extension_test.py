"""agent_pm.session.extension: the ABCs an outside Client or Host subclasses and every type their
signatures name, apart from the Session users' agent_pm.session.
"""

import inspect
import typing

from agent_pm import utils
from agent_pm.session import extension
from agent_pm.session.clients import base as clients_base
from agent_pm.session.engine import types as engine_types
from agent_pm.session.hosts import base as hosts_base
from agent_pm.session.runtimes.base import Runtime
from agent_pm.session.runtimes.modes import headless, in_process, tui

from session.tests.case import Case

ABCS = {
    "Client": clients_base,
    "Headless": headless,
    "JsonHeadless": headless,
    "Tui": tui,
    "InProcess": in_process,
    "Host": hosts_base,
    "Process": hosts_base,
    "Interactive": hosts_base,
    "InteractiveProcess": hosts_base,
    "Showable": hosts_base,
}
TYPES = {
    **dict.fromkeys(
        (
            "Spec",
            "Access",
            "Mode",
            "Effort",
            "Launch",
            "HookKind",
            "HookCallback",
            "HookCall",
            "Paste",
            "Key",
            "KeyPress",
            "NamedKey",
            "ClientBound",
            "SessionEvent",
            "Activity",
            "StatusState",
            "Blocked",
            "Exited",
            "LimitHit",
            "Liveness",
            "Reply",
            "TurnEnd",
            "TurnFailed",
            "TuiSettings",
            "Stderr",
            "SessionInfo",
            "SessionRow",
            "SessionStatus",
            "Resumable",
            "Usage",
            "Window",
            "Hosts",
            "ConfigError",
            "SessionError",
            "NotShowable",
        ),
        engine_types,
    ),
    "Record": utils,
    "Status": utils,
    "Display": utils,
}


def _named(hint) -> set:
    """The classes a type hint names, through unions and generics."""
    if isinstance(hint, type):
        return {hint}
    return {found for arg in typing.get_args(hint) for found in _named(arg)}


def _signature_types(cls: type) -> set:
    """The classes named by cls's annotations and its public methods' and __init__'s signatures."""
    hints = list(typing.get_type_hints(cls).values())
    for name, member in vars(cls).items():
        function = getattr(member, "__func__", member)
        if inspect.isfunction(function) and (not name.startswith("_") or name == "__init__"):
            hints += typing.get_type_hints(function).values()
    return {found for hint in hints for found in _named(hint)}


class ExtensionTest(Case):
    def test_all_is_the_abcs_and_their_types_each_bound_to_its_home(self):
        self.assertEqual(sorted(extension.__all__), sorted({**ABCS, **TYPES}))
        for name, home in {**ABCS, **TYPES}.items():
            with self.subTest(name=name):
                self.assertIs(getattr(extension, name), getattr(home, name))

    def test_every_type_an_abc_signature_names_is_exported(self):
        exported = {getattr(extension, n) for n in extension.__all__}
        pending = [getattr(extension, n) for n in ABCS]
        seen = set()
        while pending:
            cls = pending.pop()
            if cls in seen:
                continue
            seen.add(cls)
            for found in _signature_types(cls):
                if found.__module__.startswith("agent_pm."):
                    with self.subTest(cls=cls.__name__, found=found.__name__):
                        self.assertIn(found, exported)
                if found.__module__.startswith("agent_pm.session."):
                    pending.append(found)

    def test_no_implementation_or_running_mode_is_exported(self):
        abcs = tuple(getattr(extension, n) for n in ABCS)
        for name in extension.__all__:
            value = getattr(extension, name)
            if inspect.isclass(value) and issubclass(value, abcs):
                with self.subTest(name=name):
                    self.assertIn(name, ABCS)
                    self.assertTrue(inspect.isabstract(value) or value is extension.Client)
        self.assertNotIn(Runtime, {getattr(extension, n) for n in extension.__all__})
