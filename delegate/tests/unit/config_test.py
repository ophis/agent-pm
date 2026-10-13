"""delegate/src/agent_pm/delegate/engine/config.py: its paths; hosts(), a read delegate config's [delegates]
harness_host checked against Session.settings' hosts (their cases: session/host_tables_test.py)."""

import os
import tomllib
import unittest

import agent_pm.delegate.engine.config
from agent_pm.delegate.engine.config import CONFIG, DELEGATE, IMPORT_ROOTS, PYTHONPATH, Hosts, hosts
from agent_pm.session import Session
from agent_pm.utils.config_file import read_project

from delegate.tests import case
from delegate.tests.case import Case

TWO = """[hosts]
default = "a"

[hosts.a]
implementation = "pkg.mod:AHost"
per_column = 2

[hosts.b_2]
implementation = "pkg.b:BHost"
"""
NAMES = "a, b_2"
SETTINGS = Session.settings(tomllib.loads(TWO))


def parsed(text: str = "") -> dict:
    return tomllib.loads(text)


class PathsTest(Case):
    def test_the_delegate_dir_holds_the_config_and_the_roots_are_every_projects_src(self):
        self.assertEqual(DELEGATE, os.path.join(case.REPO, "delegate"))
        self.assertEqual(CONFIG, os.path.join(DELEGATE, "config.toml"))
        self.assertTrue(os.path.isfile(CONFIG))
        self.assertFalse(hasattr(agent_pm.delegate.engine.config, "LOCAL"))
        self.assertEqual((IMPORT_ROOTS, PYTHONPATH), (case.ROOTS, case.PYTHONPATH))

    def test_the_delegate_reads_no_hosts_or_adapters(self):
        found = read_project("delegate")
        self.assertEqual(found["delegates"], {"harness_host": ""})
        with open(CONFIG, "rb") as f:
            self.assertEqual({"hosts", "adapters"} & set(tomllib.load(f)), set())


class HostsTest(Case):
    def refused(self, config, message):
        with self.assertRaises(ValueError) as caught:
            hosts(config, SETTINGS)
        self.assertEqual(str(caught.exception), message)

    def test_the_default_its_tables_and_no_harness_host(self):
        self.assertEqual(
            hosts({}, SETTINGS),
            Hosts(
                "a",
                {
                    "a": {"implementation": "pkg.mod:AHost", "per_column": 2},
                    "b_2": {"implementation": "pkg.b:BHost"},
                },
                None,
            ),
        )
        for text in ("[delegates]\n", '[delegates]\nharness_host = ""\n'):
            with self.subTest(text):
                self.assertIsNone(hosts(parsed(text), SETTINGS).harness)

    def test_harness_host(self):
        found = hosts(parsed('[delegates]\nharness_host = "b_2"\n'), SETTINGS)
        self.assertEqual((found.default, found.harness), ("a", "b_2"))

    def test_the_committed_config_has_no_harness_host(self):
        settings = Session.settings(read_project("session"))
        self.assertIsNone(hosts(read_project("delegate"), settings).harness)

    def test_delegates_takes_only_harness_host(self):
        self.refused(
            parsed("[delegates]\nkind = 1\n"),
            "[delegates] kind: the delegate takes only harness_host",
        )
        self.refused(
            parsed('[delegates]\nharness_host = "a"\n[delegates.linear]\nx = 1\n'),
            "[delegates] linear: the delegate takes only harness_host",
        )
        self.refused({"delegates": 1}, "[delegates]: the delegate takes only harness_host")

    def test_harness_host_must_name_a_table(self):
        for value in ('"c"', "1", '"default"', "[1]"):
            with self.subTest(value=value):
                self.refused(
                    parsed(f"[delegates]\nharness_host = {value}\n"),
                    f"[delegates] harness_host: want one of {NAMES}",
                )


if __name__ == "__main__":
    unittest.main()
