import hashlib
import os

from orchestrator_test_helpers.unit.writeback.data import post
from orchestrator_test_helpers.unit.writeback.file_recheck_fixtures import (
    FileRecheck as FileRecheckBase,
)


class FileRecheck1(FileRecheckBase):
    def test_skipped(self):
        for name, make, reason in (
            ("symlink", self.symlink, "OSError: "),
            ("hard link", self.hard_link, "FileError: not a single-link file"),
            ("not md", lambda ctx: self.write(ctx, "notes.txt", "x"), "FileError: not a .md file"),
            ("outside the workdir", self.outside, "FileError: not under the workdir"),
            ("outside through a linked dir", self.linked_dir, "FileError: not under the workdir"),
            (
                "too big",
                lambda ctx: self.write(ctx, "big.md", "x" * 1_000_001),
                "FileError: over 1000000 bytes",
            ),
            ("fifo, without blocking", self.fifo, "FileError: not a regular file"),
        ):
            with self.subTest(name):
                ctx = self.ctx()
                path = make(ctx)
                gql, body = self.finish_with(ctx, path)
                self.assertEqual(
                    [c for c in gql.calls if c[0] == "comment"], [post("comment", body)]
                )
                note = f"Build failed: push not permitted\n\nCould not post the file `{os.path.basename(path)}`: {reason}"
                self.assertTrue(
                    body.startswith(note) if reason == "OSError: " else body == note, body
                )

    def test_posted(self):
        def invalid_utf8(ctx):
            path = os.path.join(ctx.workdir, "spec.md")
            with open(path, "wb") as f:
                f.write(b"ok \xff")
            return path

        for name, make, data, posted in (
            (
                "at the limit",
                lambda ctx: self.write(ctx, "big.md", "x" * 1_000_000),
                b"x" * 1_000_000,
                f"**Spec** `big.md`\n\n---\n\n{'x' * 1_000_000}",
            ),
            (
                "invalid utf-8 replaced",
                invalid_utf8,
                b"ok \xff",
                "**Spec** `spec.md`\n\n---\n\nok �",
            ),
        ):
            with self.subTest(name):
                ctx = self.ctx()
                path = make(ctx)
                gql, _ = self.finish_with(ctx, path)
                step = f"file:{os.path.basename(path)}:{hashlib.sha256(data).hexdigest()[:12]}"
                self.assertEqual(gql.calls[1], post(step, posted))
