import os
import shutil

from agent_pm.linear_orchestrator.engine import config, target
from agent_pm.utils import SHORT, checkout
from orchestrator_test_helpers.unit.target.data import LOCAL, git
from orchestrator_test_helpers.unit.target.with_clone_fixtures import WithClone as WithCloneBase
from orchestrator_test_helpers.unit.target_support import FakeRun, ok


class WithClone1(WithCloneBase):
    def test_the_checkout_is_cores_rule_with_the_issue_id(self):
        want = checkout(os.path.join(self.work, "TASK-26", "src"), "ophis", "agent-pm", "TASK-26")
        self.assertEqual(target.checkout("TASK-26", "ophis", "agent-pm", work=self.work), want)
        self.assertEqual(want, self.checkout)

    def test_a_worktree_at_the_old_owner_name_path_is_not_read(self):
        x, y = self.clone("x"), self.clone("y")
        self.worktree(x)
        os.rename(self.checkout, os.path.join(os.path.dirname(self.checkout), "agent-pm"))
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))

    def test_without_a_checkout_the_table_entry_found_case_insensitively(self):
        y = self.clone("y")
        for clones, want in (
            ({"ophis/other": "/o", "ophis/agent-pm": y}, y),
            ({"ophis/other": "/o", "Ophis/Agent-PM": y}, y),
            ({}, ""),
            ({"ophis/other": "/o", "ophis/agent-pm-2": "/p"}, ""),
        ):
            with self.subTest(clones):
                self.assertEqual(self.run_(clones), self.want(want))
        os.makedirs(os.path.join(self.work, "TASK-26", config.CLONES[0], "ophis"))
        self.assertEqual(self.run_({}), self.want(""))

    def test_a_clone_made_from_owner_name_beats_the_table(self):
        os.makedirs(self.fresh())
        self.assertEqual(self.run_({"ophis/agent-pm": self.clone("y")}), self.want(""))

    def test_a_worktree_of_the_targets_clone_beats_the_table(self):
        y = self.clone("y")
        for name, origin in (("x", self.HTTPS), ("x-scp", "git@github.com:Ophis/Agent-PM.git")):
            x = self.clone(name, origin)
            self.worktree(x)
            for clones in ({"ophis/agent-pm": y}, {}):
                with self.subTest(origin=origin, clones=clones):
                    self.assertEqual(self.run_(clones), self.want(x))

    def test_the_only_git_run_is_the_guarded_origin_read_of_the_clone(self):
        x = self.clone("x")
        self.worktree(x)
        fake = FakeRun([(LOCAL, ok(self.HTTPS + "\n"))])
        self.assertEqual(self.run_({}, run=fake), self.want(x))
        self.assertEqual(fake.calls, [((*LOCAL, x, "remote", "get-url", "origin"), SHORT)])
        for setup in (lambda: os.makedirs(self.fresh()), lambda: shutil.rmtree(self.checkout)):
            setup()
            fake.calls.clear()
            self.run_({"ophis/agent-pm": x}, run=fake)
            self.assertEqual(fake.calls, [])

    def test_a_clone_under_writable_is_not_used(self):
        x, y = self.clone("x"), self.clone("y")
        self.worktree(x)
        self.assertEqual(self.run_({"ophis/agent-pm": y}, writable=(self.tmp,)), self.want(y))
        self.assertEqual(
            self.run_({"ophis/agent-pm": y}, writable=(os.path.join(self.tmp, "elsewhere"),)),
            self.want(x),
        )
        self.assertEqual(self.run_({}, writable=(self.tmp,)), self.want(""))

    def test_the_default_writable_holds_the_runs_dir(self):
        inside = os.path.join(self.work, "clone")
        git("init", "-q", inside)
        git("-C", inside, "remote", "add", "origin", self.HTTPS)
        y = self.clone("y")
        self.worktree(inside)
        self.assertEqual(self.run_({"ophis/agent-pm": y}, writable=None), self.want(y))

    def test_the_table_decides_when_the_worktrees_clone_is_not_usable(self):
        y = self.clone("y")
        other = self.clone("other", "https://github.com/ophis/other.git")
        gone = self.clone("gone")
        self.worktree(other)
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
        self.worktree(gone)
        shutil.rmtree(gone)
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
        newline = self.clone("x\ny")
        self.worktree(newline)
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
        self.git_file("nonsense\n")
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
        self.git_file("gitdir: " + os.path.join(self.tmp, "no-such-gitdir") + "\n")
        self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))

    def test_a_symlinked_git_is_not_followed(self):
        x, y = self.clone("x"), self.clone("y")
        real_file = os.path.join(self.tmp, "dotgit-file")
        self.worktree(x)
        shutil.copy(os.path.join(self.checkout, ".git"), real_file)
        for dest in (real_file, os.path.join(x, ".git")):
            with self.subTest(dest):
                os.symlink(dest, self.fresh())
                self.assertEqual(self.run_({"ophis/agent-pm": y}), self.want(y))
                self.assertEqual(self.run_({}), self.want(""))
