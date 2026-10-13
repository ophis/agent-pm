"""Safety behavior."""

import io
import json
import os
import shutil
import subprocess

from agent_pm.delegate.team.tools.types import RepoServices

from delegate.tests.unit.tools.repo.case import Clone, Real, fail, git, info, repo


class LocalSafety(Clone):
    def worktree(self, fail_on=None, temp=(), config=None, spec=None, local=None):
        run, out, err = (
            Real([info(), *([(fail_on, fail("boom"))] if fail_on else [])]),
            io.StringIO(),
            io.StringIO(),
        )
        code = repo.main(
            ["worktree", "--dir", self.dir, "--branch", "TASK-1-x", spec or self.clone],
            config=config or self.none,
            local=local or self.none,
            temp=temp,
            services=RepoServices(run=run, out=out, err=err),
        )
        return code, err.getvalue()

    def file_commit(self):
        with open(os.path.join(self.seed, "f"), "w") as fh:
            fh.write("one\n")
        git("-C", self.seed, "add", "f")
        git("-C", self.seed, "commit", "-q", "-m", "f")
        git("-C", self.seed, "push", "-q", "origin", "main")
        git("-C", self.clone, "fetch", "-q")

    def link_commit(self):
        os.symlink("/etc/passwd", os.path.join(self.seed, "link"))
        git("-C", self.seed, "add", "link")
        git("-C", self.seed, "commit", "-q", "-m", "link")
        git("-C", self.seed, "push", "-q", "origin", "main")
        return os.path.join(self.wt, "link")

    def assertGood(self):
        index = git("-C", self.wt, "rev-parse", "--path-format=absolute", "--git-path", "index")
        self.assertTrue(os.path.isfile(index))
        self.assertTrue(os.path.isfile(os.path.join(self.wt, "f")))
        self.assertEqual(git("-C", self.wt, "status", "--porcelain"), "")
        self.assertEqual(
            git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "false"
        )

    def symlinks(self, wt=None):
        """Worktree `wt`'s own core.symlinks, else the clone's; None when unset."""
        res = subprocess.run(
            [
                "git",
                "-C",
                wt or self.clone,
                "config",
                *(["--worktree"] if wt else []),
                "--get",
                "core.symlinks",
            ],
            capture_output=True,
            text=True,
        )
        return res.stdout.strip() if res.returncode == 0 else None

    def remove(self):
        shutil.rmtree(self.wt, ignore_errors=True)
        git("-C", self.clone, "worktree", "prune")

    def half_made(self):
        """A worktree whose checkout never finished."""
        git("-C", self.clone, "config", "extensions.worktreeConfig", "true")
        git(
            "-C",
            self.clone,
            "worktree",
            "add",
            "-q",
            "--no-checkout",
            "-b",
            "TASK-1-x",
            self.wt,
            "origin/main",
        )

    def test_a_local_clone_under_a_temp_dir_is_refused(self):
        code, err = self.worktree(temp=(self.tmp,))
        self.assertEqual(code, 2)
        self.assertIn("temp dir", err)
        self.assertFalse(os.path.exists(self.wt))

    def test_a_failed_step_after_the_add_leaves_no_worktree_and_a_rerun_succeeds(self):
        self.file_commit()
        for step, fail_on in (
            ("config", lambda a: a[3:5] == ["config", "--worktree"] and "--get" not in a),
            ("reset", lambda a: a[3:4] == ["reset"]),
        ):
            with self.subTest(step):
                code, err = self.worktree(fail_on=fail_on)
                self.assertEqual(code, 1, err)
                self.assertFalse(os.path.exists(self.wt))
                self.assertNotIn(self.wt, git("-C", self.clone, "worktree", "list", "--porcelain"))
                self.assertEqual(self.worktree(), (0, ""))
                self.assertGood()
                self.remove()

    def test_a_half_made_worktree_is_made_again(self):
        self.file_commit()
        self.half_made()
        self.assertEqual(self.worktree(), (0, ""))
        self.assertGood()

    def test_a_half_made_worktree_of_a_listed_clone_is_made_again_with_symlinks(self):
        link = self.link_commit()
        git("-C", self.clone, "fetch", "-q")
        self.half_made()
        self.assertEqual(self.worktree(config=self.listing(self.clone)), (0, ""))
        self.assertEqual(
            git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "true"
        )
        self.assertEqual(os.readlink(link), "/etc/passwd")

    def test_only_a_listed_clones_worktree_gets_symlinks_and_the_clone_keeps_its_config(self):
        """trusted_dirs lists clones by real path, in the config or its local file; listing another
        clone of the same origin trusts no other.
        """
        link = self.link_commit()
        other, alias = os.path.join(self.tmp, "clone2"), os.path.join(self.tmp, "alias")
        git("clone", "-q", self.bare, other)
        os.symlink(self.clone, alias)
        local = os.path.join(self.tmp, "config.local.toml")
        for case, listed, in_local, spec, on in (
            ("none listed", None, False, None, False),
            ("listed", [self.clone], False, None, True),
            ("another clone of the origin listed", [other], False, None, False),
            ("listed by an alias", [alias], False, None, True),
            ("listed, given by an alias", [self.clone], False, alias, True),
            ("listed in the local file", [], True, None, True),
        ):
            with self.subTest(case):
                if in_local:
                    with open(local, "w") as f:
                        f.write(f"trusted_dirs = {json.dumps([self.clone])}\n")
                try:
                    config = None if listed is None else self.listing(*listed)
                    self.assertEqual(self.worktree(config=config, spec=spec, local=local), (0, ""))
                    if on:
                        self.assertEqual(os.readlink(link), "/etc/passwd")
                    else:
                        self.assertTrue(os.path.isfile(link) and not os.path.islink(link))
                    for scope in (["--worktree"], []):  # set per worktree, and so in effect
                        self.assertEqual(
                            git("-C", self.wt, "config", *scope, "--get", "core.symlinks"),
                            str(on).lower(),
                        )
                    self.assertEqual(git("-C", self.wt, "status", "--porcelain"), "")
                    self.assertIsNone(self.symlinks())
                    self.assertEqual(git("-C", self.clone, "status", "--porcelain"), "")
                finally:
                    self.remove()
                    if in_local:
                        os.remove(local)

    def test_an_existing_worktree_must_have_the_listed_setting(self):
        """Made by git (unset), unlisted (false) or listed (true): run with the other setting,
        refused and unchanged.
        """
        self.file_commit()
        listed = self.listing(self.clone)
        for made, run_config, found, gives in (
            ("by git", None, "unset", "false"),
            ("unlisted", listed, "false", "true"),
            ("listed", None, "true", "false"),
        ):
            with self.subTest(made=made):
                if made == "by git":
                    git(
                        "-C",
                        self.clone,
                        "worktree",
                        "add",
                        "-q",
                        "-b",
                        "TASK-1-x",
                        self.wt,
                        "origin/main",
                    )
                else:
                    self.assertEqual(
                        self.worktree(config=listed if made == "listed" else None), (0, "")
                    )
                code, err = self.worktree(config=run_config)
                self.assertEqual(code, 2, err)
                for text in (
                    f"core.symlinks={found}",
                    f"trusted_dirs gives {gives}",
                    "remove it and run again",
                ):
                    self.assertIn(text, err)
                self.assertEqual(self.symlinks(self.wt), None if found == "unset" else found)
                self.remove()
        self.assertEqual(self.worktree(config=listed), (0, ""))
        self.assertEqual(self.worktree(config=listed), (0, ""))

    def test_a_worktree_deleted_by_hand_is_added_again(self):
        other = os.path.join(self.tmp, "users-own")
        git("-C", self.clone, "worktree", "add", "-q", "-b", "mine", other)
        shutil.rmtree(other)
        self.assertEqual(self.worktree()[0], 0)
        shutil.rmtree(self.wt)
        self.assertEqual(self.worktree(), (0, ""))
        self.assertEqual(git("-C", self.wt, "branch", "--show-current"), "TASK-1-x")
        self.assertIn(f"worktree {other}", git("-C", self.clone, "worktree", "list", "--porcelain"))

    def test_a_mirror_refspec_cannot_move_the_clones_branches(self):
        git("-C", self.clone, "branch", "TASK-9-mine")
        mine = git("-C", self.clone, "rev-parse", "TASK-9-mine")
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "theirs")
        git("-C", self.seed, "push", "-q", "origin", "HEAD:TASK-9-mine")
        git("-C", self.clone, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        self.assertEqual(self.worktree()[0], 0)
        self.assertEqual(git("-C", self.clone, "rev-parse", "TASK-9-mine"), mine)
