"""config.py: hosts(), a merged core config's [hosts] tables and core [delegates]."""

import tomllib
import unittest

from agent_pm.config import Hosts, hosts

from new.tests.case import Case

TWO = """[hosts]
default = "a"

[hosts.a]
implementation = "pkg.mod:AHost"
per_column = 2

[hosts.b_2]
implementation = "pkg.b:BHost"
"""
NAMES = "a, b_2"


def parsed(text: str = "", base: str = TWO) -> dict:
    return tomllib.loads(base + text)


class HostsTest(Case):
    def refused(self, config, message):
        with self.assertRaises(ValueError) as caught:
            hosts(config)
        self.assertEqual(str(caught.exception), message)

    def test_the_default_its_tables_and_no_harness_host(self):
        self.assertEqual(
            hosts(parsed()),
            Hosts(
                "a",
                {
                    "a": {"implementation": "pkg.mod:AHost", "per_column": 2},
                    "b_2": {"implementation": "pkg.b:BHost"},
                },
                None,
            ),
        )
        self.assertIsNone(hosts(parsed("[delegates]\n")).harness)

    def test_harness_host(self):
        found = hosts(parsed('[delegates]\nharness_host = "b_2"\n'))
        self.assertEqual((found.default, found.harness), ("a", "b_2"))

    def test_the_tables_are_copies(self):
        config = parsed()
        hosts(config).tables["a"]["per_column"] = 3
        self.assertEqual(config["hosts"]["a"]["per_column"], 2)

    def test_no_hosts_table(self):
        for config in ({}, {"hosts": 1}, {"hosts": "a"}):
            with self.subTest(config=config):
                self.refused(config, "[hosts]: missing")

    def test_a_key_that_is_neither_default_nor_a_table(self):
        self.refused(
            parsed(base=TWO.replace('default = "a"', 'default = "a"\nnoise = 1')),
            "[hosts] noise: want default or a [hosts.<name>] table",
        )

    def test_a_table_name_that_is_not_a_host_name(self):
        for key, name in (("A", "A"), ("1a", "1a"), ("a-b", "a-b"), ('"a b"', "a b"), ('""', "")):
            with self.subTest(name=name):
                self.refused(
                    parsed(f'[hosts.{key}]\nimplementation = "pkg.c:CHost"\n'),
                    f"[hosts.{name}]: want a name matching [a-z][a-z0-9_]*",
                )

    def test_a_table_without_a_class_path(self):
        for line in ("", 'implementation = "pkg.mod.AHost"\n', "implementation = 1\n"):
            with self.subTest(line=line):
                self.refused(
                    parsed(base=TWO.replace('implementation = "pkg.mod:AHost"\n', line)),
                    "[hosts.a].implementation: want module.path:Class",
                )

    def test_the_default_must_name_a_table(self):
        for line in ("", 'default = "c"', "default = 1", 'default = ""', "default = []"):
            with self.subTest(line=line):
                self.refused(
                    parsed(base=TWO.replace('default = "a"', line)),
                    f"[hosts] default: want one of {NAMES}",
                )

    def test_core_delegates_takes_only_harness_host(self):
        self.refused(
            parsed("[delegates]\nkind = 1\n"), "[delegates] kind: core takes only harness_host"
        )
        self.refused(
            parsed('[delegates]\nharness_host = "a"\n[delegates.linear]\nx = 1\n'),
            "[delegates] linear: core takes only harness_host",
        )
        self.refused({**parsed(), "delegates": 1}, "[delegates]: core takes only harness_host")

    def test_harness_host_must_name_a_table(self):
        for value in ('"c"', "1", '""', '"default"', "[]"):
            with self.subTest(value=value):
                self.refused(
                    parsed(f"[delegates]\nharness_host = {value}\n"),
                    f"[delegates] harness_host: want one of {NAMES}",
                )


if __name__ == "__main__":
    unittest.main()
