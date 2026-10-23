"""Installer path inventory and cleanup, without launching tools."""

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.case import REPO, Case


def helper(script="install.sh"):
    text = Path(REPO, "scripts", script).read_text()
    body = text.split("agent_tree_clean() {\n", 1)[1].split("\n}\n", 1)[0]
    return body.split("python3 -I -c '\n", 1)[1].split("' ", 1)[0]


class InstallCleanupTest(Case):
    def setUp(self):
        self.root = Path(self.home, "agent-root")
        self.root.mkdir()
        self.state = self.root / "install"
        self.state.mkdir()
        self.snapshot = self.state / "tree.paths"

    def baseline(self, *relative):
        paths = [self.root, *(self.root / path for path in relative)]
        self.snapshot.write_bytes(b"".join(os.fsencode(path) + b"\0" for path in paths))

    def clean(self):
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["-c", str(self.root), str(self.snapshot)]),
            redirect_stdout(output),
        ):
            exec(helper(), {})
        return output.getvalue().strip()

    def test_shared_helper(self):
        self.assertEqual(helper(), helper("install-cleanup.sh"))

    def test_prunes_new_children_and_preserves_original_files_and_board(self):
        original = self.root / "work/user-project/keep.txt"
        original.parent.mkdir(parents=True)
        original.write_text("user data")
        self.baseline("work", "work/user-project", "work/user-project/keep.txt")
        new = self.root / "work/user-project/new.txt"
        new.write_text("runtime")
        for relative in ("logs/runs.jsonl", ".runtime/cache", "board/unassigned/issue.md"):
            path = self.root / relative
            path.parent.mkdir(parents=True)
            path.write_text("runtime")
        self.assertEqual(self.clean(), "keep")
        self.assertEqual(original.read_text(), "user data")
        self.assertFalse(new.exists())
        self.assertFalse((self.root / "logs").exists())
        self.assertFalse((self.root / ".runtime").exists())
        self.assertTrue((self.root / "board/unassigned/issue.md").exists())
        self.assertTrue(self.snapshot.exists())

    def test_symlinks_are_opaque_and_preexisting_dangling_links_stay(self):
        old = self.root / "work"
        old.symlink_to(self.root / "missing")
        outside = Path(self.home, "outside")
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("user data")
        self.baseline("work")
        new = self.root / "logs"
        new.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.clean(), "keep")
        self.assertTrue(old.is_symlink())
        self.assertFalse(new.is_symlink())
        self.assertEqual(sentinel.read_text(), "user data")

    def test_empty_original_root_is_empty_after_clean(self):
        self.baseline()
        (self.root / "runtime").write_text("runtime")
        self.assertEqual(self.clean(), "")
        self.assertEqual(list(self.root.iterdir()), [self.state])

    def test_cleanup_failure_stops_without_overwriting_snapshot(self):
        self.baseline()
        before = self.snapshot.read_bytes()
        with (
            mock.patch.object(os, "scandir", side_effect=OSError("private detail")),
            self.assertRaises(SystemExit) as raised,
        ):
            self.clean()
        self.assertNotIn("private detail", str(raised.exception))
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_invalid_snapshot_stops_before_deletion(self):
        self.snapshot.write_text('{"invalid":"snapshot"}')
        runtime = self.root / "runtime"
        runtime.write_text("keep on failure")
        with self.assertRaises(SystemExit):
            self.clean()
        self.assertEqual(runtime.read_text(), "keep on failure")
