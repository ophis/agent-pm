import json
import os

from orchestrator_test_helpers.unit.config.data import CORE_LOCAL, write
from orchestrator_test_helpers.unit.config.team_dirs_ignored_fixtures import (
    TeamDirsIgnored as TeamDirsIgnoredBase,
)


class TeamDirsIgnored1(TeamDirsIgnoredBase):
    def test_team_dirs_change_no_orchestrator_run(self):
        before = self.seen()
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            f"team_dirs = {json.dumps([self.team])}\n" + CORE_LOCAL + "[roles.writer]\ntier = 3\n",
        )
        after = self.seen()
        self.assertEqual(after, before)
        self.assertNotIn("one-pager", after[1].prompt)
        self.assertFalse(os.path.exists(self.work))
