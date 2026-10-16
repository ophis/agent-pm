"""Host process and display interfaces and defaults, SIGNALS; Host.parse_hosts, the one [hosts]
parser.
"""

import inspect
import operator
import signal
import tomllib
from types import MappingProxyType
from unittest import mock

from agent_pm.session import (
    SIGNALS,
    ConfigError,
    Displayable,
    Host,
    HostProcess,
    Hosts,
    SessionError,
    Terminal,
)

from session.tests.case import Case

TWO = """[hosts]
default = "a"

[hosts.a]
implementation = "pkg.mod:AHost"
per_column = 2

[hosts.b_2]
implementation = "pkg.b:BHost"
"""
NAMES = "a, b_2"

PROCESS = {"alive", "close", "wait_exit", "kill", "record", "display"}

ABSTRACT = {
    Host: {"detach", "terminal", "attach", "list"},
    HostProcess: PROCESS,
    Terminal: PROCESS | {"send", "press"},
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

    def test_displayable_placement_is_keyword_only(self):
        key, either = inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD
        for method, parameters in (
            (
                Displayable.show,
                [("self", either), ("name", either), ("display", key), ("command", key)],
            ),
            (Displayable.layout, [("self", either), ("display", key)]),
        ):
            with self.subTest(method=method.__name__):
                found = inspect.signature(method).parameters.values()
                self.assertEqual([(p.name, p.kind) for p in found], parameters)


class HostDefaults(Case):
    def test_a_host_reclaims_nothing_and_a_process_neither_publishes_nor_has_a_token(self):
        self.assertIsNone(MinimalHost("m").reclaim("/tmp/apm-group-x"))
        process = mock.create_autospec(HostProcess, instance=True)
        process.name = "p"
        self.assertIsNone(HostProcess.token.fget(process))
        with self.assertRaisesRegex(SessionError, "^session p: its host cannot store a Record"):
            HostProcess.publish(process, mock.sentinel.record)

    def test_descendants_are_its_listed_sessions_unless_it_knows_who_opened_whom(self):
        host = MinimalHost("m")
        with mock.patch.object(host, "list", return_value=["a", "b"]) as listed:
            self.assertEqual(host.descendants("d"), ["a", "b"])
        listed.assert_called_once_with("d")

    def test_name_and_a_copy_of_settings(self):
        settings = {"root": "/x"}
        host = MinimalHost("m", settings)
        self.assertEqual((host.name, host.settings), ("m", settings))
        self.assertIsNot(host.settings, settings)
        self.assertEqual(MinimalHost("m").settings, {})

    def test_each_host_gets_its_own_mutable_settings_even_from_read_only_ones(self):
        given = MappingProxyType({"sizes": (1, [2]), "grid": MappingProxyType({"a": 1})})
        first, second = MinimalHost("a", given), MinimalHost("b", given)
        first.settings["sizes"][1].append(3)
        first.settings["grid"]["b"] = 2
        self.assertEqual(second.settings, {"sizes": (1, [2]), "grid": {"a": 1}})

    def test_display_returns_its_value_and_display_help_is_empty(self):
        host = MinimalHost("m")
        self.assertFalse(isinstance(host, Displayable))
        self.assertEqual(host.display("elsewhere:1.2"), "elsewhere:1.2")
        self.assertIsNone(host.display(None))
        self.assertEqual(host.display_help(), "")

    def test_signals_a_detached_run_blocks_are_hup_term_and_int(self):
        self.assertEqual(SIGNALS, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))


def table(base: str = TWO, text: str = ""):
    return tomllib.loads(base + text).get("hosts")


class ParseHosts(Case):
    def refused(self, found, message):
        with self.assertRaises(ConfigError) as caught:
            Host.parse_hosts(found)
        self.assertEqual(str(caught.exception), message)

    def test_the_default_and_its_tables(self):
        self.assertEqual(
            Host.parse_hosts(table()),
            Hosts(
                "a",
                {
                    "a": {"implementation": "pkg.mod:AHost", "per_column": 2},
                    "b_2": {"implementation": "pkg.b:BHost"},
                },
            ),
        )

    def test_the_tables_are_read_only_deep_copies(self):
        found = {**table(), "c": {"implementation": "pkg.c:CHost", "sizes": [1]}}
        hosts = Host.parse_hosts(found)
        found["a"]["per_column"] = 3
        found["c"]["sizes"].append(2)
        found["d"] = {"implementation": "pkg.d:DHost"}
        self.assertEqual((hosts.tables["a"]["per_column"], hosts.tables["c"]["sizes"]), (2, (1,)))
        self.assertNotIn("d", hosts.tables)
        for mapping, key in ((hosts.tables, "d"), (hosts.tables["a"], "per_column")):
            with self.subTest(key=key), self.assertRaises(TypeError):
                operator.setitem(mapping, key, 3)

    def test_a_malformed_hosts_table_is_refused_naming_the_part(self):
        names = (("A", "A"), ("1a", "1a"), ("a-b", "a-b"), ('"a b"', "a b"), ('""', ""))
        cases = (
            ({**table(), 1: {"implementation": "pkg.c:CHost"}}, "[hosts] 1: want a string key"),
            *((found, "[hosts]: missing") for found in (None, 1, "a", [])),
            (
                table(TWO.replace('default = "a"', 'default = "a"\nnoise = 1')),
                "[hosts] noise: want default or a [hosts.<name>] table",
            ),
            *(
                (
                    table(text=f'[hosts.{key}]\nimplementation = "pkg.c:CHost"\n'),
                    f"[hosts.{name}]: want a name matching [a-z][a-z0-9_]*",
                )
                for key, name in names
            ),
            *(
                (
                    table(TWO.replace('implementation = "pkg.mod:AHost"\n', line)),
                    "[hosts.a].implementation: want module.path:Class",
                )
                for line in ("", 'implementation = "pkg.mod.AHost"\n', "implementation = 1\n")
            ),
            *(
                (table(TWO.replace('default = "a"', line)), f"[hosts] default: want one of {NAMES}")
                for line in ("", 'default = "c"', "default = 1", 'default = ""', "default = []")
            ),
        )
        for found, message in cases:
            with self.subTest(found=found, message=message):
                self.refused(found, message)

    def test_the_class_is_never_loaded(self):
        found = table(TWO.replace("pkg.mod:AHost", "agent_pm.session.hosts.tmux:TmuxHost"))
        with mock.patch("importlib.import_module") as imp:
            self.assertEqual(Host.parse_hosts(found).default, "a")
        imp.assert_not_called()
