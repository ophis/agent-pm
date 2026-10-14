"""Subprocess environment validation."""

from types import MappingProxyType

from agent_pm.utils import environment_ok

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
