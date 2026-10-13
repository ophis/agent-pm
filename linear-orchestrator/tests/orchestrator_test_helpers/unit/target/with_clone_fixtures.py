import os
import shutil
import tempfile

from agent_pm.linear_orchestrator.engine import config, target
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.target.data import git


class WithClone(Case):
    T = target.Target("ophis", "agent-pm", "TASK-26-x")

    HTTPS = "https://github.com/ophis/agent-pm.git"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.work = os.path.join(self.tmp, "work")
        self.checkout = os.path.join(
            self.work, "TASK-26", config.CLONES[0], "ophis", "agent-pm-TASK-26"
        )

    def clone(self, name, origin=HTTPS):
        path = os.path.join(self.tmp, name)
        git("init", "-q", path)
        git("-C", path, "remote", "add", "origin", origin)
        return path

    def fresh(self):
        shutil.rmtree(self.checkout, ignore_errors=True)
        os.makedirs(self.checkout)
        return os.path.join(self.checkout, ".git")

    def git_file(self, text):
        with open(self.fresh(), "w") as f:
            f.write(text)

    def worktree(self, clone):
        admin = os.path.join(self.tmp, "admin")
        os.makedirs(admin, exist_ok=True)
        with open(os.path.join(admin, "commondir"), "w") as f:
            f.write(os.path.join(clone, ".git") + "\n")
        self.git_file(f"gitdir: {admin}\n")
        return admin

    def run_(self, clones, **kw):
        kw.setdefault("writable", ())
        return target.with_clone(self.T, "TASK-26", clones, work=self.work, **kw)

    def want(self, clone):
        return target.Target("ophis", "agent-pm", "TASK-26-x", clone)
