from agent_pm.linear_orchestrator.engine import target
from orchestrator_test_helpers.unit.target.data import PROJ, issue
from orchestrator_test_helpers.unit.target.research_repo_fixtures import (
    ResearchRepo as ResearchRepoBase,
)


class ResearchRepo1(ResearchRepoBase):
    def test_mapped_repo_only_without_a_repo_line(self):
        for desc, project, repos, want in (
            ("no repo here", PROJ, self.REPOS, target.Target("ophis", "agent-pm")),
            ("", PROJ, self.REPOS, target.Target("ophis", "agent-pm")),
            ("Repo: ophis/other", PROJ, self.REPOS, None),
            ("Repo: nope", PROJ, self.REPOS, None),
            ("Repo: ophis/a\nRepo: ophis/b", PROJ, self.REPOS, None),
            ("no repo here", "other", self.REPOS, None),
            ("no repo here", None, self.REPOS, None),
            ("no repo here", PROJ, {}, None),
        ):
            with self.subTest(desc=desc, project=project, repos=repos):
                self.assertEqual(target.research_repo(issue(desc, project=project), repos), want)
