"""new/core/src/utils/git.py: common_dir over real git worktrees."""
import os
import subprocess
import tempfile
import unittest
import unittest.mock

from new.tests import SRC  # noqa: F401
from utils.git import common_dir


def git(*argv):
    return subprocess.run(["git", *argv], check=True, capture_output=True, text=True).stdout.strip()


class CommonDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        env = unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                                                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        env.start()
        self.addCleanup(env.stop)
        self.clone, self.bare = os.path.join(self.tmp, "clone"), os.path.join(self.tmp, "origin.git")
        git("init", "-q", "-b", "main", self.clone)
        git("-C", self.clone, "commit", "-q", "--allow-empty", "-m", "one")
        git("clone", "-q", "--bare", self.clone, self.bare)

    def add(self, branch="TASK-1-x", opts=()):
        wt = os.path.join(self.tmp, "w", branch)
        git("-C", self.clone, "worktree", "add", "-q", *opts, "-b", branch, wt)
        return wt

    def test_a_worktree_names_its_clones_git_dir(self):
        common = os.path.join(self.clone, ".git")
        self.assertEqual(common_dir(self.add()), common)
        self.assertEqual(common_dir(self.add("TASK-1-rel", ("--relative-paths",))), common)

    def test_a_crlf_git_file_is_read_as_git_reads_it(self):
        wt = self.add()
        gitdir = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        with open(os.path.join(wt, ".git"), "w", newline="") as f:
            f.write(f"gitdir: {gitdir}\r\n")
        self.assertEqual(common_dir(wt), os.path.join(self.clone, ".git"))

    def test_trailing_whitespace_is_part_of_the_path_as_for_git(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        fake, other = os.path.join(self.tmp, "fake-admin"), os.path.join(self.tmp, "other", ".git")
        os.makedirs(fake)
        os.makedirs(other)
        with open(os.path.join(fake, "commondir"), "w") as f:
            f.write(other + "\n")
        a = os.path.join(self.tmp, "a")
        os.makedirs(a)
        os.symlink(admin, os.path.join(a, "sym"))
        os.symlink(fake, os.path.join(a, "sym "))
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write(f"gitdir: {a}/sym \n")
        self.assertEqual(common_dir(wt), other)

    def test_no_worktree_names_no_clone(self):
        wt = self.add()
        plain, linked, junk, orphan, bare = (os.path.join(self.tmp, d) for d in ("plain", "linked", "junk", "orphan", "bare"))
        for d in (plain, linked, junk, orphan, bare):
            os.makedirs(d)
        os.symlink(os.path.join(wt, ".git"), os.path.join(linked, ".git"))
        for d, text in ((junk, "nonsense\n"), (orphan, f"gitdir: {self.tmp}/gone\n"), (bare, f"gitdir: {self.bare}\n")):
            with open(os.path.join(d, ".git"), "w") as f:
                f.write(text)
        for path in (self.clone, plain, linked, junk, orphan, bare, os.path.join(self.tmp, "missing")):
            with self.subTest(path=path):
                self.assertIsNone(common_dir(path))


if __name__ == "__main__":
    unittest.main()
