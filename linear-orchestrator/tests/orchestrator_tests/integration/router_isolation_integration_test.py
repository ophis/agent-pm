"""Real Git and interpreter contracts in the test sandbox."""

import json
import os

from agent_pm.utils import GUARD, SHORT
from orchestrator_tests.router_fixtures import (
    DESIGN_LISTING,
    ENG_RUN,
    INPUT,
    LISTING,
    LS_REMOTE,
    OUTER,
    ROLE,
    OuterBase,
)
from orchestrator_tests.unit.run_fixtures import (
    ID,
    RESEARCHER,
)
from orchestrator_tests.unit.target_fixtures import ok

from delegate.tests.workdir import checkout_path
from utils.tests.sandbox import git


class RunLaunching(OuterBase):
    def local_clone(self):
        path = os.path.realpath(os.path.join(self.tmp, "clone"))
        for argv in (
            ["init", "-q", path],
            ["-C", path, "remote", "add", "origin", "https://github.com/ophis/agent-pm.git"],
        ):
            git(*argv)
        self.write_config(OUTER + f'[local_clones]\n"Ophis/Agent-PM" = "{path}"\n')
        return path

    def test_a_configured_local_clone_is_the_inputs_repo(self):
        clone = self.local_clone()
        self.assertEqual(self.outer(), 0)
        self.assertEqual(self.input(), INPUT.replace("Repo: Ophis/Agent-PM", f"Repo: {clone}"))
        self.assertEqual(
            self.run_.calls, [(("gh", "api", "repos/ophis/agent-pm"), SHORT), (LS_REMOTE, SHORT)]
        )
        self.assertEqual(self.err, "")

    def test_research_and_design_name_the_local_clone(self):
        clone = self.local_clone()
        for who_, role, listing in (
            (RESEARCHER, "researcher", LISTING),
            (ROLE["pm"], "pm", DESIGN_LISTING),
        ):
            with self.subTest(who=who_):
                self.run_.table = [(listing, ok("[]"))]
                self.assertEqual(self.outer(assignee=who_), 0)
                self.assertEqual(self.detached(role).input.splitlines()[1], f"Repo: {clone}")
                self.assertIsNone(json.loads(self.raw_runs().splitlines()[-1])["target"])

    def test_a_resume_over_a_clone_checkout_keeps_its_repo(self):
        self.local_clone()
        cfg = self.cfg()
        found = checkout_path(cfg.issue_workdir(ID), "Ophis", "Agent-PM", ID)
        os.makedirs(os.path.join(found, ".git"))
        self.api.checkouts[cfg.issue_workdir(ID)] = (found,)
        self.run_.table = [(("git", *GUARD, "-C"), ok()), *ENG_RUN]
        self.assertEqual(self.outer(mode="resume"), 0)
        self.assertEqual(self.input(), INPUT)
        self.assertEqual([argv[0] for argv, _ in self.run_.calls], ["gh", "git", "git"])
