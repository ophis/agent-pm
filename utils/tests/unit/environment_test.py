"""Subprocess environment validation and the agent session marker."""

import os
from types import MappingProxyType
from unittest import mock

from agent_pm.utils import AGENT_SESSION_ENV, environment_ok, nested_start

from utils.tests.case import Case


class Environment(Case):
    def test_accepts_exec_names_and_string_values(self):
        env = {"A-B": "", "1A": "a=b", "X;Y": "a\nb", "A B": "v", "Ä": "v"}
        self.assertTrue(environment_ok({}))
        self.assertTrue(environment_ok(env))
        self.assertTrue(environment_ok(MappingProxyType(env)))

    def test_rejects_invalid_environment_shapes_names_and_values(self):
        for env in (
            None,
            [("A", "v")],
            {"": "v"},
            {"A=B": "v"},
            {"A\0B": "v"},
            {1: "v"},
            {"A": 1},
            {"A": None},
            {"A": "v\0"},
        ):
            with self.subTest(env=env):
                self.assertFalse(environment_ok(env))


class NestedStart(Case):
    def test_none_outside_an_agent_session_and_the_reason_inside_one(self):
        self.assertEqual(AGENT_SESSION_ENV, "AGENT_PM_AGENT_SESSION")
        with mock.patch.dict(os.environ, clear=True):
            self.assertIsNone(nested_start())
            os.environ[AGENT_SESSION_ENV] = ""
            self.assertIsNone(nested_start())
            os.environ[AGENT_SESSION_ENV] = "1"
            self.assertEqual(
                nested_start(),
                "refused inside an agent session agent-pm started ($AGENT_PM_AGENT_SESSION is set): "
                "only a client you start starts sessions and runs",
            )
