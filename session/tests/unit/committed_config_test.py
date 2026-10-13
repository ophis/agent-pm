"""session/config.toml's committed [adapters] tables: each builds its Adapter through the session
registry.
"""

import os
import tempfile
import unittest
from unittest import mock

from agent_pm.session import Driver, Spec
from agent_pm.session.adapters import types as registry
from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.print_prompt import PrintPromptAdapter
from agent_pm.utils import read_project

from session.tests.case import Case, settings

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def committed() -> dict:
    return registry.settings(read_project("session", local=os.devnull))["adapters"]


class Committed(Case):
    def test_the_committed_adapters_build_print_prompt(self):
        self.driver = Driver(**settings(committed()))
        self.assertEqual(self.driver.registry.configured(), ["claude", "codex", "print_prompt"])
        for name, table in committed().items():
            with self.subTest(name=name):
                self.assertEqual({"roles", "output"} & set(table), set())
        self.assertIs(type(self.driver.registry.adapter("print_prompt")), PrintPromptAdapter)

    def test_the_claude_table_builds_a_claude_adapter(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(tmp.name, "config")})
        patch.start()
        self.addCleanup(patch.stop)
        tables = committed()
        self.assertEqual(
            tables["claude"],
            {
                "implementation": "adapters.claude:ClaudeAdapter",
                "flags": [],
                "allow": [],
                "env": {},
                "tiers": {"1": "fable", "2": "opus", "3": "sonnet", "4": "haiku"},
                "efforts": {e: e for e in ("low", "medium", "high", "xhigh", "max")},
            },
        )
        self.driver = Driver(**settings(tables))
        adapter = self.driver.registry.adapter("claude")
        self.assertIs(type(adapter), ClaudeAdapter)
        argv = (
            adapter.headless()
            .launch("hi", Spec(workdir=tmp.name, tier=4, effort="low", sid=SID), False)
            .argv
        )
        self.assertEqual(
            argv[4:10],
            ("--model", "haiku", "--effort", "low", "--setting-sources", "user"),
        )


if __name__ == "__main__":
    unittest.main()
