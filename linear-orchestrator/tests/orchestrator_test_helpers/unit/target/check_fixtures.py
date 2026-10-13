import os
import shutil
import tempfile

from agent_pm.linear_orchestrator.engine import config, target
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.target.data import LOCAL, PROJ, issue, roles
from orchestrator_test_helpers.unit.target_support import FakeRun, api, ok


class Check(Case):
    REPOS = {PROJ: "ophis/agent-pm"}

    MAPPED = {"desc": "no repo here", "repos": REPOS, "project": PROJ}

    ISSUES = (
        ("Repo line", {}, ""),
        ("Repo line, mapped project", {"repos": REPOS, "project": PROJ}, ""),
        ("mapped project", MAPPED, "project mapping "),
    )

    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.clone = os.path.join(
            self.work, "TASK-26", config.CLONES[0], "ophis", "agent-pm-TASK-26"
        )
        self.ls_remote = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "ls-remote",
            "--heads",
            "https://github.com/ophis/agent-pm.git",
            "TASK-26-*",
        ]

    def git_dir(self):
        os.makedirs(os.path.join(self.clone, ".git"))

    def git_file(self):
        os.makedirs(self.clone)
        with open(os.path.join(self.clone, ".git"), "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/agent-pm\n")

    def branch_list(self, at=None):
        at = at or self.clone
        return (
            *LOCAL,
            at,
            f"--git-dir={at}/.git",
            "branch",
            "--list",
            "TASK-26-*",
            "--format=%(refname:short)",
        )

    def check(
        self,
        desc="Repo: ophis/agent-pm",
        repos=None,
        project=None,
        title="ENG: Session Registry",
        **over,
    ):
        t = {"api": api(), "local": ok(""), "remote": ok("")}
        t.update(over)
        self.run_ = FakeRun(
            [
                (("gh", "api"), t["api"]),
                (LOCAL, t["local"]),
                (("git", "-c", "credential.helper="), t["remote"]),
            ]
        )
        return target.check(
            issue(desc, title=title, project=project),
            repos or {},
            run=self.run_,
            work=self.work,
            roles=roles(),
        )

    def argvs(self):
        return [c[0] for c in self.run_.calls]
