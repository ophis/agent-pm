"""Checkout discovery and common directories against isolated Git repositories."""

import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest import mock

from agent_pm.utils import GUARD, LOCAL_ENV_VARS, Checkouts, find_checkouts, overlaps
from agent_pm.utils.git import common_dir

from utils.tests.case import Case
from utils.tests.sandbox import clean_env, git, git_run


class CheckoutDiscovery(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.env = clean_env()
        self.env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))
        self.outer = self.root / "outer"
        self.git("init", "-q", "-b", "main", str(self.outer))
        self.git("-C", str(self.outer), "commit", "-q", "--allow-empty", "-m", "initial")

    @staticmethod
    def runner(argv, timeout, env):
        return git_run(*argv[1:], timeout=timeout, env=env, stdin=subprocess.DEVNULL)

    def git(self, *argv):
        result = self.runner(["git", *GUARD, *argv], 20, self.env)
        result.check_returncode()
        return result.stdout

    def search(self, path):
        return find_checkouts(str(path), runner=self.runner, timeout=20)

    def test_not_a_checkout_nested_checkouts_and_missing_paths(self):
        self.assertEqual(self.search(self.root / "missing"), Checkouts(()))
        inner = self.outer / "inner"
        self.git("init", "-q", str(inner))
        self.assertEqual(
            self.search(inner / "missing" / "child"), Checkouts((str(inner), str(self.outer)))
        )
        alias = self.root / "alias"
        alias.symlink_to(inner)
        self.assertEqual(self.search(alias / "missing"), self.search(inner))

    def test_bare_repositories_are_skipped_without_losing_the_containing_checkout(self):
        for bare, expected in (
            (self.root / "bare", Checkouts(())),
            (self.outer / "bare", Checkouts((str(self.outer),))),
        ):
            self.git("init", "-q", "--bare", str(bare))
            with self.subTest(bare=bare):
                self.assertEqual(self.search(bare), expected)
                self.assertEqual(self.search(bare / "objects" / "missing"), expected)

    def test_submodule_discovers_its_superproject(self):
        source = self.root / "source"
        self.git("init", "-q", str(source))
        self.git("-C", str(source), "commit", "-q", "--allow-empty", "-m", "initial")
        self.git(
            "-c",
            "protocol.file.allow=always",
            "-C",
            str(self.outer),
            "submodule",
            "add",
            "-q",
            str(source),
            "module",
        )
        self.assertEqual(
            self.search(self.outer / "module"),
            Checkouts((str(self.outer / "module"), str(self.outer))),
        )

    def test_linked_worktree_reports_the_working_tree_and_its_outer_checkout(self):
        source = self.root / "source"
        self.git("clone", "-q", str(self.outer), str(source))
        linked = self.outer / "linked"
        self.git("-C", str(source), "worktree", "add", "-q", "-b", "linked", str(linked))
        self.assertEqual(self.search(linked / "missing"), Checkouts((str(linked), str(self.outer))))

    def test_repository_selection_variables_cannot_redirect_discovery(self):
        poison = dict.fromkeys(LOCAL_ENV_VARS, str(self.root / "absent"))
        with mock.patch.dict(os.environ, poison):
            self.assertEqual(self.search(self.outer), Checkouts((str(self.outer),)))
        self.assertEqual(
            set(self.git("-C", str(self.outer), "rev-parse", "--local-env-vars").splitlines()),
            LOCAL_ENV_VARS,
        )

    def test_corrupt_git_directory_and_gitfile_are_errors(self):
        for name, is_file in (("gitdir", False), ("gitfile", True)):
            broken = self.root / name
            broken.mkdir()
            marker = broken / ".git"
            if is_file:
                marker.write_text("gitdir: missing-admin\n")
            else:
                marker.mkdir()
            result = self.search(broken)
            with self.subTest(name=name):
                self.assertEqual(result.roots, ())
                self.assertIsNotNone(result.error)
                self.assertIn(str(broken), result.error)

    def test_unreadable_metadata_is_an_error(self):
        marker = self.outer / ".git"
        marker.chmod(0)
        try:
            result = self.search(self.outer)
        finally:
            marker.chmod(0o755)
        self.assertEqual(result.roots, ())
        self.assertIsNotNone(result.error)
        self.assertIn(str(self.outer), result.error)

    def test_case_aliases_overlap_and_discover_the_same_checkout(self):
        variant = self.root / "OUTER"
        if not variant.exists():
            self.skipTest("requires a case-insensitive filesystem")
        self.assertTrue(os.path.samefile(self.outer, variant))
        self.assertTrue(overlaps(str(self.outer), str(variant / "missing" / "child")))
        self.assertTrue(overlaps(str(variant / "missing"), str(self.outer / "missing" / "child")))
        self.assertFalse(overlaps(str(variant / "missing"), str(self.outer / "other")))
        result = self.search(variant / "missing")
        self.assertIsNone(result.error)
        self.assertEqual(len(result.roots), 1)
        self.assertTrue(os.path.samefile(result.roots[0], self.outer))


class CommonDir(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        env = unittest.mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.clone, self.bare = (
            os.path.join(self.tmp, "clone"),
            os.path.join(self.tmp, "origin.git"),
        )
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
        plain, linked, junk, orphan, bare = (
            os.path.join(self.tmp, d) for d in ("plain", "linked", "junk", "orphan", "bare")
        )
        for d in (plain, linked, junk, orphan, bare):
            os.makedirs(d)
        os.symlink(os.path.join(wt, ".git"), os.path.join(linked, ".git"))
        for d, text in (
            (junk, "nonsense\n"),
            (orphan, f"gitdir: {self.tmp}/gone\n"),
            (bare, f"gitdir: {self.bare}\n"),
        ):
            with open(os.path.join(d, ".git"), "w") as f:
                f.write(text)
        for path in (
            self.clone,
            plain,
            linked,
            junk,
            orphan,
            bare,
            os.path.join(self.tmp, "missing"),
        ):
            with self.subTest(path=path):
                self.assertIsNone(common_dir(path))

    def test_a_commondir_symlink_is_followed(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        path = os.path.join(admin, "commondir")
        target = path + "-target"
        os.rename(path, target)
        os.symlink(target, path)
        self.assertEqual(common_dir(wt), os.path.join(self.clone, ".git"))

    def test_a_file_replaced_by_a_fifo_at_open_is_rejected_without_waiting(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        open_file = os.open
        for path in (os.path.join(wt, ".git"), os.path.join(admin, "commondir")):
            with self.subTest(path=path):
                backup = path + "-backup"

                def replace(file, flags, *args, target=path, **kwargs):
                    if file == target:
                        self.assertTrue(flags & os.O_NONBLOCK)
                        os.rename(target, target + "-backup")
                        os.mkfifo(target)
                    return open_file(file, flags, *args, **kwargs)

                with unittest.mock.patch("os.open", side_effect=replace):
                    self.assertIsNone(common_dir(wt))
                os.unlink(path)
                os.rename(backup, path)

    def test_a_git_file_replaced_by_a_symlink_at_open_is_rejected(self):
        wt = self.add()
        path = os.path.join(wt, ".git")
        target = path + "-target"
        open_file = os.open

        def replace(file, flags, *args, **kwargs):
            if file == path:
                os.rename(path, target)
                os.symlink(target, path)
            return open_file(file, flags, *args, **kwargs)

        with unittest.mock.patch("os.open", side_effect=replace):
            self.assertIsNone(common_dir(wt))
