"""session/hosts/tables.py: parse(), an already-read [hosts] table's default and [hosts.<name>]
tables, as parse_hosts gives it.
"""

import tomllib
import unittest
from unittest import mock

from agent_pm.session import ConfigError, Session
from agent_pm.session.hosts.tables import Hosts, parse

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


def table(base: str = TWO, text: str = ""):
    return tomllib.loads(base + text).get("hosts")


class ParseTest(Case):
    def refused(self, found, message):
        with self.assertRaises(ValueError) as caught:
            parse(found)
        self.assertEqual(str(caught.exception), message)

    def test_the_default_and_its_tables(self):
        self.assertEqual(
            parse(table()),
            Hosts(
                "a",
                {
                    "a": {"implementation": "pkg.mod:AHost", "per_column": 2},
                    "b_2": {"implementation": "pkg.b:BHost"},
                },
            ),
        )

    def test_session_parse_hosts_is_this_parser(self):
        self.assertEqual(Session.parse_hosts(table()), parse(table()))
        with self.assertRaisesRegex(ConfigError, r"^\[hosts\]: missing$"):
            Session.parse_hosts(None)

    def test_a_non_string_key_is_a_value_error(self):
        found = {**table(), 1: {"implementation": "pkg.c:CHost"}}
        self.refused(found, "[hosts] 1: want a string key")

    def test_the_tables_are_copies(self):
        found = table()
        parse(found).tables["a"]["per_column"] = 3
        self.assertEqual(found["a"]["per_column"], 2)

    def test_no_hosts_table(self):
        for found in (None, 1, "a", []):
            with self.subTest(found=found):
                self.refused(found, "[hosts]: missing")

    def test_a_key_that_is_neither_default_nor_a_table(self):
        self.refused(
            table(TWO.replace('default = "a"', 'default = "a"\nnoise = 1')),
            "[hosts] noise: want default or a [hosts.<name>] table",
        )

    def test_a_table_name_that_is_not_a_host_name(self):
        for key, name in (("A", "A"), ("1a", "1a"), ("a-b", "a-b"), ('"a b"', "a b"), ('""', "")):
            with self.subTest(name=name):
                self.refused(
                    table(text=f'[hosts.{key}]\nimplementation = "pkg.c:CHost"\n'),
                    f"[hosts.{name}]: want a name matching [a-z][a-z0-9_]*",
                )

    def test_a_table_without_a_class_path(self):
        for line in ("", 'implementation = "pkg.mod.AHost"\n', "implementation = 1\n"):
            with self.subTest(line=line):
                self.refused(
                    table(TWO.replace('implementation = "pkg.mod:AHost"\n', line)),
                    "[hosts.a].implementation: want module.path:Class",
                )

    def test_the_default_must_name_a_table(self):
        for line in ("", 'default = "c"', "default = 1", 'default = ""', "default = []"):
            with self.subTest(line=line):
                self.refused(
                    table(TWO.replace('default = "a"', line)),
                    f"[hosts] default: want one of {NAMES}",
                )

    def test_the_class_is_never_loaded(self):
        found = table(TWO.replace("pkg.mod:AHost", "agent_pm.session.hosts.tmux:TmuxHost"))
        with (
            mock.patch("importlib.import_module") as imp,
        ):
            self.assertEqual(parse(found).default, "a")
        imp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
