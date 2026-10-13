from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.unit.config.repo_slug_fixtures import RepoSlug as RepoSlugBase


class RepoSlug1(RepoSlugBase):
    def test_slug(self):
        cases = {
            "ophis/agent-pm": ("ophis", "agent-pm"),
            "ophis/.github": ("ophis", ".github"),
            "-a/b": None,
            "a/-b": None,
            "a/..": None,
            "a/.": None,
            "a b/c": None,
            "ophis": None,
            "https://github.com/ophis/x": None,
            "": None,
            None: None,
            42: None,
        }
        for value, want in cases.items():
            with self.subTest(value=value):
                self.assertEqual(config.repo_slug(value), want)
