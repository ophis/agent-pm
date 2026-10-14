"""The tmux grid hooks' tile command: parsing and injected dispatch."""

import io
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from agent_pm.session.hosts.tmux import cli, types
from agent_pm.session.hosts.tmux import grid as grids

from session.tests.case import Case
from session.tests.tmux_fixtures import (
    FakeTmux,
)


class CliTest(Case):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(list(argv), operations=getattr(self, "actions", {}))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def patch(self, name, **kw):
        if not hasattr(self, "actions"):
            self.actions = {}
        self.actions[name] = unittest.mock.Mock(**kw)
        return self.actions[name]

    def test_help(self):
        rc, out, _ = self.main("-h")
        self.assertEqual(rc, 0)
        self.assertIn("{tile}", out)
        self.assertIn("By hand: agent-pm-session tile", " ".join(out.replace("-\n", "-").split()))

    def test_tile(self):
        real, fake = grids.tile, FakeTmux()
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
        tile.side_effect = lambda window, wait, if_panes_changed: real(
            window, wait=wait, if_panes_changed=if_panes_changed, proc=fake
        )
        self.assertEqual(
            self.main("tile", "3"), (1, "", "tui: invalid window id '3': want @[0-9]+\n")
        )
        self.assertEqual(fake.calls, [])

    def test_exit_1(self):
        self.patch("tile", side_effect=types.TuiError("tmux: boom"))
        self.assertEqual(self.main("tile", "@3"), (1, "", "tui: tmux: boom\n"))

    def test_exit_2(self):
        tile = self.patch("tile")
        for argv in (
            (),
            ("bogus",),
            ("start", "a"),
            ("tile",),
            ("tile", "--no-wait"),
            ("tile", "@3", "@4"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 2)
        tile.assert_not_called()
