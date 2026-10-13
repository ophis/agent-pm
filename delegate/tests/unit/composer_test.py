import os
import types

from agent_pm.delegate import composer
from agent_pm.delegate.engine.config import CONFIG

from delegate.tests.case import REPO, Case
from delegate.tests.unit.composer.shipped import (
    API,
    DROPPED,
)


class Package(Case):
    def test_the_api_is_the_modules_names(self):
        self.assertEqual(composer.__all__, API)
        self.assertEqual([name for name in DROPPED if hasattr(composer, name)], [])
        self.assertIs(composer.render, composer.prompt.render)
        self.assertIs(composer.resolve, composer.run.resolve)
        self.assertIsInstance(composer.teams, types.ModuleType)
        self.assertIs(composer.team, composer.teams.team)
        self.assertIs(composer.team_paths, composer.teams.team_paths)
        self.assertEqual(CONFIG, os.path.join(REPO, "delegate", "config.toml"))
