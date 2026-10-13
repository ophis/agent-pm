"""Host process and display interfaces and defaults."""

import inspect

from agent_pm.session import (
    Displayable,
    Host,
    HostProcess,
    Terminal,
)

from session.tests.case import Case

PROCESS = {"alive", "close", "wait_exit", "kill", "record", "display"}

ABSTRACT = {
    Host: {"detach", "terminal", "attach", "list"},
    HostProcess: PROCESS,
    Terminal: PROCESS | {"send"},
    Displayable: {"show", "attach_command", "screen", "layout", "tile"},
}


class MinimalHost(Host):
    def detach(self, *args, **kwargs):
        raise AssertionError("not called")

    terminal = attach = list = detach


class Abstract(Case):
    def test_each_interface_is_abstract_in_its_methods(self):
        for cls, methods in ABSTRACT.items():
            with self.subTest(cls=cls.__name__):
                self.assertTrue(inspect.isabstract(cls))
                self.assertEqual(cls.__abstractmethods__, methods)

    def test_a_terminal_is_a_host_process(self):
        self.assertTrue(issubclass(Terminal, HostProcess))


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
