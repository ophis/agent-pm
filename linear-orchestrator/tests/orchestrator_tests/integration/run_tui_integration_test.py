"""run.py --tui as a subprocess through the Delegate's tui mode, on a private tmux server
(live_tmux) whose manager session a client shows, fake_claude interactive as runtime `claude`: FR-43
closes only this role and issue's old TUI; the TUI `<role>-<ID>-<sid8>` shows beside the
--split-from session, or in the --opener's grid; the run writes back and leaves its TUI open.
"""

import functools
import unittest

from orchestrator_tests.integration import fake_linear
from orchestrator_tests.integration.flow_fixtures import ACCOUNTS, PROJECT, RUN, Flow, steps
from orchestrator_tests.unit.run_fixtures import ID, SID, UUID
from orchestrator_tests.unit.writeback_fixtures import PRD

from delegate.tests.replies import block, mark
from session.tests import live_tmux

MANAGER = "manager"
TUI = f"pm-{ID}-{SID[:8]}"
OLD = f"pm-{ID}-aaaaaaaa"
KEPT = (f"{OLD}-drive", f"engineer-{ID}-aaaaaaaa")
LEDGER = ["start", "retitle", "comment", f"attach:{PRD}", "move:in_review"]
DONE = {
    "status": "done",
    "title": "Session Registry",
    "summary": "PRD for the registry.",
    "url": PRD,
}


class Tui(Flow):
    def setUp(self):
        super().setUp()
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.bin = self.server.bin
        self.guard()
        self.fake.issue(
            "Session registry",
            seed=fake_linear.IssueSeed(
                identifier=ID,
                id=UUID,
                state="in_progress",
                assignee=self.ids[ACCOUNTS["pm"]],
                project=PROJECT,
            ),
        )
        self.scene([f"{mark('start', 'a PRD.')}\n{block(DONE)}"])

    def base(self):
        return self.server.env()

    def live(self, session):
        return self.server.tmux("has-session", "-t", f"={session}").returncode == 0

    def run_tui(self, *argv):
        res = self.script(
            *RUN,
            *("--uuid", UUID, "--issue", ID, "--project", PROJECT["id"], "--sid", SID),
            *("--assignee", ACCOUNTS["pm"], "--runtime", "claude", "--mode", "new", "--tui"),
            *argv,
            "--input",
            "Do it.\n",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.live(TUI), res.stderr)
        self.assertEqual((self.state(ID), self.ledger(ID)), ("in_review", {SID: LEDGER}))

    def test_old_tuis_of_this_role_and_issue_close_and_the_tui_shows_beside_split_from(self):
        for name in (OLD, *KEPT):
            made = self.server.tmux("new-session", "-d", "-s", name, *live_tmux.IDLE)
            self.assertEqual(made.returncode, 0, made.stderr)
        self.run_tui("--split-from", MANAGER)
        self.assertEqual([self.live(n) for n in (OLD, *KEPT)], [False, True, True])
        self.assertEqual(self.said(), steps(*LEDGER, tuis=[f"run tui-closed {ID} session={OLD}"]))
        panes, pane = self.server.panes(f"={MANAGER}:"), self.server.pane_of(TUI)
        self.assertEqual(set(panes), {self.manager, pane})
        self.assertGreater(panes[pane][0], panes[self.manager][0])

    def test_the_tui_shows_in_the_openers_grid(self):
        self.run_tui("--opener", MANAGER)
        self.assertEqual(self.said(), steps(*LEDGER))
        live_tmux.wait(
            functools.partial(live_tmux.assert_grid, self, self.server, MANAGER, [[TUI]]),
            what=f"the grid [[{TUI!r}]]",
        )


if __name__ == "__main__":
    unittest.main()
