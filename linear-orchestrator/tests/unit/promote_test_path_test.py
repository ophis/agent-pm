import os
from unittest import mock

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.unit.promote.test_path_fixtures import TestPath as TestPathBase


class TestPath1(TestPathBase):
    def test_main_sets_path_first(self):
        seen = []

        def spy(query, **v):
            seen.append(os.environ["PATH"])
            return self.fake(query, **v)

        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            self.run_main("--dry-run", gql=spy)
            self.assertEqual(os.environ["PATH"], config.path())
        self.assertEqual(set(seen), {config.path()})
        self.assertIn("/opt/homebrew/bin", config.path())
