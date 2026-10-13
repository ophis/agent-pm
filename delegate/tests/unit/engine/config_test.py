"""delegate/src/agent_pm/delegate/engine/config.py: its paths; harness_host(), a read delegate
config's [delegates] harness_host checked against Session.parse_hosts' names (their cases:
session/host_tables_test.py); policy()'s shape (plan's Policy cases: through plan) and
trusted_dirs().
"""

import os
import tomllib
import unittest
from unittest import mock

import agent_pm.delegate.engine.config
from agent_pm.delegate.engine.config import (
    CONFIG,
    DELEGATE,
    IMPORT_ROOTS,
    PYTHONPATH,
    harness_host,
    policy,
    trusted_dirs,
)
from agent_pm.session import Session
from agent_pm.utils import read_project

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
HOSTS = Session.parse_hosts(tomllib.loads(TWO)["hosts"]).tables


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


class HarnessHostTest(Case):
    def refused(self, config, message):
        with self.assertRaises(ValueError) as caught:
            harness_host(config, HOSTS)
        self.assertEqual(str(caught.exception), message)

    def test_unset_is_none(self):
        for text in ("", "[delegates]\n", '[delegates]\nharness_host = ""\n'):
            with self.subTest(text):
                self.assertIsNone(harness_host(parsed(text), HOSTS))

    def test_harness_host(self):
        self.assertEqual(harness_host(parsed('[delegates]\nharness_host = "b_2"\n'), HOSTS), "b_2")

    def test_the_committed_config_has_no_harness_host(self):
        hosts = Session.parse_hosts(read_project("session")["hosts"]).tables
        self.assertIsNone(harness_host(read_project("delegate"), hosts))

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


class PolicyTest(Case):
    def test_a_runtimes_flags_and_env_else_none(self):
        cfg = parsed('[policy.codex]\nflags = ["-s"]\nenv = { K = "v" }\n')
        self.assertEqual(policy("codex", cfg), (["-s"], {"K": "v"}))
        self.assertEqual(policy("fake", cfg), ([], {}))

    def test_a_malformed_table_is_a_value_error(self):
        for cfg in (
            {"policy": 1},
            {"policy": {"c": 1}},
            {"policy": {"c": {"x": 1}}},
            {"policy": {"c": {"flags": "-s"}}},
        ):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                policy("c", cfg)


class TrustedDirsTest(Case):
    def test_unset_is_empty_and_paths_are_real_paths(self):
        self.assertEqual(trusted_dirs({}), frozenset())
        with mock.patch.dict(os.environ, {"HOME": "/var/h"}):
            got = trusted_dirs({"trusted_dirs": ["~/c", "/x/../y"]})
        self.assertEqual(got, {os.path.realpath("/var/h/c"), os.path.realpath("/y")})


if __name__ == "__main__":
    unittest.main()
