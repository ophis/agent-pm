"""Tmux cli behavior at standard-library boundaries."""

import ast
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    RUNTIME,
    Tmux,
)


class CliTest(Case):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = tmux_host.main(list(argv), operations=getattr(self, "actions", {}))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def patch(self, name, **kw):
        if not hasattr(self, "actions"):
            self.actions = {}
        self.actions[name] = unittest.mock.Mock(**kw)
        return self.actions[name]

    def test_start_forms(self):
        # the command verbatim; start shows nothing, so it takes no show option
        start = self.patch("start")
        for argv, session, command, events in (
            ("start a -- cmd", "a", ["cmd"], None),
            ("start a -- cmd -- x", "a", ["cmd", "--", "x"], None),
            ("start a --events e -- cmd --settings {}", "a", ["cmd", "--settings", "{}"], "e"),
            ("start --events e b -- cmd --x -- -p", "b", ["cmd", "--x", "--", "-p"], "e"),
        ):
            with self.subTest(argv=argv):
                start.reset_mock()
                self.assertEqual(self.main(*argv.split())[0], 0)
                start.assert_called_once_with(
                    session,
                    tmux_host.Command(argv=command, cwd=os.getcwd(), env=dict(os.environ)),
                    options=tmux_host.StartOptions(events=events),
                )

    def test_start_usage_errors(self):
        start = self.patch("start")
        for argv in (
            "start a",
            "start a --",
            "start a --bogus x -- cmd",
            "start a=b -- cmd",
            "start a cmd",
            "start -- cmd",
            "start a b -- cmd",
            "start --events -- a -- cmd",
            "start --show T a -- cmd",
            "start --split below a -- cmd",
            "start --split-from b a -- cmd",
            "start --per-column 2 a -- cmd",
            "start --grid-retile off a -- cmd",
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv.split())[0], 2)
        for argv in ("start a", "start a cmd", "start a --events e cmd"):
            with self.subTest(argv=argv):
                self.assertIn("a command must follow --", self.main(*argv.split())[2])
        start.assert_not_called()

    @unittest.mock.patch.dict(
        os.environ, {"NO_COLOR": "1"}
    )  # beats FORCE_COLOR, which colors argparse's help (3.14)
    def test_help(self):
        rc, out, _ = self.main("-h")
        self.assertEqual(rc, 0)
        self.assertIn("{start,send,read,show,tile}", out)
        self.assertIn("$TUI_ATTACH_PREFIX", out)
        rc, out, _ = self.main("start", "-h")
        self.assertEqual(rc, 0)
        out = " ".join(out.split())
        self.assertIn("session -- command", out)
        self.assertIn("--events FILE", out)
        for option in ("--show", "--split", "--per-column", "--grid-retile"):
            self.assertNotIn(option, out)
        for cmd in ((), ("start",), ("send",), ("read",), ("show",), ("tile",)):
            with self.subTest(cmd=cmd):
                self.assertIsNone(RUNTIME.search(self.main(*cmd, "-h")[1]))

    def test_subcommands(self):
        send, read, show, status = (self.patch(n) for n in ("send", "read", "show", "status"))
        read.return_value, show.return_value, status.return_value = (
            "pane\ntext",
            None,
            tmux_host.RUNNING,
        )
        self.assertEqual(self.main("send", "s", "hi there"), (0, "", ""))
        send.assert_called_once_with("s", "hi there")
        self.assertEqual(self.main("send", "s", "--", "-x")[0], 0)
        self.assertEqual(send.call_args, unittest.mock.call("s", "-x"))
        self.assertEqual(self.main("read", "s"), (0, "pane\ntext\n", ""))
        self.assertEqual(self.main("read", "s", "--lines", "3")[0], 0)
        self.assertEqual(
            read.call_args_list, [unittest.mock.call("s", None), unittest.mock.call("s", 3)]
        )
        self.assertEqual(
            self.main("show", "--show", "x", "--split", "below", "--split-from", "b", "s"),
            (0, "", ""),
        )
        status.assert_called_once_with("s")
        show.assert_called_once_with(
            "s",
            "x",
            options=tmux_host.PaneOptions(
                split="below", split_from="b", per_column=3, retile="open-close"
            ),
        )
        self.main("show", "s")
        self.assertEqual(
            show.call_args,
            unittest.mock.call(
                "s", None, options=tmux_host.PaneOptions(per_column=3, retile="open-close")
            ),
        )

    @unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    def test_per_column(self):
        show = self.patch("show", return_value=None)
        self.patch("status", return_value=tmux_host.RUNNING)
        for value, n in (("1", 1), ("2", 2), ("9999", 9999), ("0003", 3)):
            with self.subTest(value=value):
                self.assertEqual(self.main("show", "--per-column", value, "s")[0], 0)
                self.assertEqual(show.call_args.kwargs["options"].per_column, n)
        show.reset_mock()
        for value in ("0", "10000", "00000", "-1", "x", "", "2.0", "\u00b2", " 2"):
            with self.subTest(value=value):
                rc, _, err = self.main("show", "--per-column", value, "s")
                self.assertEqual(rc, 2)
                self.assertIn("--per-column", err)
        show.assert_not_called()
        self.assertIn("--per-column N", " ".join(self.main("show", "-h")[1].split()))

    @unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    def test_grid_retile(self):
        show = self.patch("show", return_value=None)
        self.patch("status", return_value=tmux_host.RUNNING)
        for mode in ("all", "open-close", "off"):
            with self.subTest(mode=mode):
                self.assertEqual(self.main("show", "--grid-retile", mode, "s")[0], 0)
                self.assertEqual(show.call_args.kwargs["options"].retile, mode)
        show.reset_mock()
        for value in ("x", "", "ALL", "open_close", " off"):
            with self.subTest(value=value):
                rc, _, err = self.main("show", "--grid-retile", value, "s")
                self.assertEqual(rc, 2)
                self.assertIn("--grid-retile", err)
        show.assert_not_called()
        self.assertIn("--grid-retile M", " ".join(self.main("show", "-h")[1].split()))

    def test_tile(self):
        real, fake = tmux_host.tile, Tmux()
        tile = self.patch("tile", return_value=None)
        self.assertEqual(self.main("tile", "@3"), (0, "", ""))
        self.assertEqual(self.main("tile", "--no-wait", "@3"), (0, "", ""))
        self.assertEqual(self.main("tile", "--no-wait", "--if-panes-changed", "@3"), (0, "", ""))
        self.assertEqual(self.main("tile", "--if-panes-changed", "@3"), (0, "", ""))
        call = unittest.mock.call
        self.assertEqual(
            tile.call_args_list,
            [
                call("@3", wait=True, if_panes_changed=False),
                call("@3", wait=False, if_panes_changed=False),
                call("@3", wait=False, if_panes_changed=True),
                call("@3", wait=True, if_panes_changed=True),
            ],
        )
        self.assertIn("--if-panes-changed", self.main("tile", "-h")[1])
        tile.side_effect = lambda window, wait, if_panes_changed: real(window, wait=wait, proc=fake)
        self.assertEqual(
            self.main("tile", "3"), (1, "", "tui: invalid window id '3': want @[0-9]+\n")
        )
        self.assertEqual(fake.calls, [])

    def test_exit_1(self):
        for name in ("start", "send", "read", "tile"):
            self.patch(name, side_effect=tmux_host.TuiError("tmux: boom"))
        for argv in ("start a -- cmd", "send s hi", "read s", "tile @3"):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv.split()), (1, "", "tui: tmux: boom\n"))
        status, show = (
            self.patch("status", return_value=None),
            self.patch("show", return_value="boom"),
        )
        self.assertEqual(self.main("show", "s"), (1, "", "tui: no session s\n"))
        show.assert_not_called()
        status.return_value = tmux_host.RUNNING
        self.assertEqual(self.main("show", "s")[0], 1)

    def test_exit_2(self):
        called = [self.patch(n) for n in ("start", "send", "read", "show", "status", "tile")]
        for argv in (
            (),
            ("bogus",),
            ("send", "s"),
            ("send", "a b", "x"),
            ("read", "s", "--lines", "0"),
            ("read", "s", "--lines", "x"),
            ("read", "s", "--lines", "-1"),
            ("show", "--split", "up", "s"),
            ("show", "--split-from", "a b", "s"),
            ("tile",),
            ("tile", "--no-wait"),
            ("tile", "@3", "@4"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 2)
        for mock in called:
            mock.assert_not_called()


class SingleFileTest(Case):
    def setUp(self):
        with open(tmux_host.__file__) as f:
            self.source = f.read()

    def test_imports_only_the_stdlib(self):
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add("." * node.level + (node.module or "").split(".")[0])
        self.assertLessEqual(names, set(sys.stdlib_module_names))

    def test_no_runtime_name(self):
        self.assertEqual(RUNTIME.findall(self.source), [])

    def test_runs_alone(self):
        with tempfile.TemporaryDirectory() as d:
            bundle = shutil.copytree(os.path.dirname(tmux_host.__file__), os.path.join(d, "host"))
            copy = os.path.join(bundle, "tmux_host.py")
            res = subprocess.run(
                [sys.executable, "-I", copy, "--help"],
                cwd=d,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
        self.assertEqual(res.returncode, 0, res.stderr)
        for word in ("start", "send", "read", "show"):
            self.assertIn(word, res.stdout)
