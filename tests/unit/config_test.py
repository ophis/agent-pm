"""config.py: hosts(), a merged core config's [hosts] tables (their cases: session/host_tables_test.py) and core
[delegates]; session_settings(), Session.configure's keyword arguments."""

import os
import tomllib
import unittest
from unittest import mock

import agent_pm.config
from agent_pm.config import CONFIG, Hosts, hosts, session_settings
from agent_pm.session.hosts import tables
from agent_pm.utils.config_file import read_config

from tests.case import Case

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

    def test_the_hosts_table_goes_through_the_session_parser(self):
        config = parsed()
        with mock.patch.object(tables, "parse", wraps=tables.parse) as parse:
            hosts(config)
        parse.assert_called_once_with(config["hosts"])
        with self.assertRaisesRegex(ValueError, r"\A\[hosts\]: missing\Z"):
            hosts({})

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


class SessionSettingsTest(Case):
    def test_the_adapters_hosts_and_default_of_a_merged_config(self):
        config = parsed('[adapters.a]\nimplementation = "adapters.a:AAdapter"\n')
        self.assertEqual(
            session_settings(config),
            {
                "adapters": {"a": {"implementation": "adapters.a:AAdapter"}},
                "hosts": hosts(config).tables,
                "default_host": "a",
            },
        )
        self.assertEqual(session_settings(parsed())["adapters"], {})

    def test_unset_it_reads_the_core_config_and_local_file(self):
        with open(os.path.join(self.agent_pm, "core-new.local.toml"), "w") as f:
            f.write("[hosts.tmux]\nstatus_line = true\n")
        found = session_settings()
        self.assertEqual(found, session_settings(read_config(CONFIG, agent_pm.config.LOCAL)))
        self.assertEqual(sorted(found["adapters"]), ["claude", "codex", "print_prompt"])
        self.assertEqual(found["default_host"], "tmux")
        self.assertIs(found["hosts"]["tmux"]["status_line"], True)

    def test_an_unreadable_core_config_is_a_value_error(self):
        with open(os.path.join(self.agent_pm, "core-new.local.toml"), "w") as f:
            f.write("x = \n")
        with self.assertRaisesRegex(ValueError, f"\\Acore config {CONFIG}: "):
            session_settings()


if __name__ == "__main__":
    unittest.main()
