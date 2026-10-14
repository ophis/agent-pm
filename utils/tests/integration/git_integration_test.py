"""Checkout discovery against isolated real repositories; no caller git environment is inherited."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from agent_pm.utils import GUARD, LOCAL_ENV_VARS, Checkouts, find_checkouts

from utils.tests.case import Case


class CheckoutDiscovery(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
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
        return subprocess.run(
            argv, timeout=timeout, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )

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
