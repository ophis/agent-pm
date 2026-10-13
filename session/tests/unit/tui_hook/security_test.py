"""Security behaviour."""

import json
import os
import stat

from session.tests.unit.tui_hook.case import MISSING, HookBase, write_state


class SecurityTest(HookBase):
    def test_bad_arguments_write_nothing(self):
        for args in (
            [],
            [self.dir],
            [self.dir, "stop", "x"],
            ["s1", "stop"],
            [self.dir + "\n", "stop"],
            [self.dir, ""],
            [self.dir, "a b"],
            [self.dir, "x" * 65],
            [self.dir, "../x"],
        ):
            with self.subTest(args):
                self.assertError(self.hook(None, args=args), "usage")
        self.assertNothingWritten()

    def test_a_bad_session_dir_writes_nothing(self):
        afile = os.path.join(self.root, "file")
        open(afile, "w").close()
        for folder in (os.path.join(self.root, "missing"), afile):
            with self.subTest(folder):
                self.assertError(self.hook(None, args=[folder, "stop"]), "session.json")
        self.assertNothingWritten()

    def test_a_session_json_that_is_no_regular_file_writes_nothing(self):
        path = os.path.join(self.dir, "session.json")
        real = os.path.join(self.root, "real")
        os.mkdir(real)
        write_state(real, self.state)
        os.unlink(path)
        self.assertError(self.hook("stop"), "session.json")
        for make in (
            lambda: os.symlink(os.path.join(real, "session.json"), path),
            lambda: os.mkfifo(path),
            lambda: os.mkdir(path),
        ):
            make()
            with self.subTest(stat.filemode(os.lstat(path).st_mode)):
                self.assertError(self.hook("stop"), "session.json")
            (os.rmdir if os.path.isdir(path) and not os.path.islink(path) else os.unlink)(path)
        self.assertNothingWritten()

    def test_each_invalid_session_state_field_writes_nothing(self):
        bad = {
            "words": (
                [],
                "x",
                None,
                {"a b": "done"},
                {"": "done"},
                {"x" * 65: "done"},
                {"a/b": "done"},
                {"stop": "dead"},
                {"stop": None},
                {"stop": ["done"]},
            ),
            "events": ("", "events", "/a\nb", 1, []),
            "sid": ("", "a/b", "a\tb", 1, None),
            "runtime": ("", "Fake", "1x", "a-b", "a b", None),
            "workdir": ("", "rel", "/a\x00b", None),
            "cwd": ("rel", "/a\nb", None),
            "transcript": ("rel", "/a\x7fb", None),
            "project": ("true", 0, 1, None),
            "resume": (None, 1, []),
            "tmux_path": ("", "tmux", "bin/tmux", "/a\nb", None, 1),
        }
        for field, values in bad.items():
            for value in (*values, MISSING):
                with self.subTest(field=field, value=value):
                    changed = {k: v for k, v in self.state.items() if k != field}
                    if value is not MISSING:
                        changed[field] = value
                    write_state(self.dir, changed)
                    self.assertError(self.hook("stop"), "session.json")
                    self.assertNothingWritten()

    def test_a_session_json_not_an_object_of_at_most_1_mib_writes_nothing(self):
        for data in (
            b"",
            b"not json",
            b"[]",
            b'"s"',
            b"\xff",
            json.dumps({**self.state, "pad": "x" * (1 << 20)}).encode(),
        ):
            with self.subTest(data[:20]):
                write_state(self.dir, data)
                self.assertError(self.hook("stop"), "session.json")
        self.assertNothingWritten()

    def test_hooks_jsonl_that_is_no_regular_file_is_refused_and_nothing_marked(self):
        target = os.path.join(self.root, "target")
        open(target, "w").close()
        for make in (
            lambda: os.symlink(target, self.hooks),
            lambda: os.mkfifo(self.hooks),
            lambda: os.mkdir(self.hooks),
        ):
            make()
            with self.subTest(stat.filemode(os.lstat(self.hooks).st_mode)):
                self.assertError(self.hook("stop"), "hooks.jsonl")
            (
                os.rmdir
                if os.path.isdir(self.hooks) and not os.path.islink(self.hooks)
                else os.unlink
            )(self.hooks)
        with open(target) as f:
            self.assertEqual(f.read(), "")
        self.assertNothingWritten()
