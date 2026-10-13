from agent_pm.session import DriverError
from orchestrator_test_helpers.unit.run.data import MATCH, TUI
from orchestrator_test_helpers.unit.run.tui_fixtures import Tui as TuiBase
from orchestrator_test_helpers.unit.run_fixtures import SID, Gql


class Tui1(TuiBase):
    def test_old_tuis_of_this_role_and_issue_close_before_the_run(self):
        for extra in ((), ("--tui",)):
            with self.subTest(extra=extra):
                self.gql, self.api.live = Gql(), [*self.MINE, *self.OTHER]
                self.order.clear()
                n = len(self.said())
                self.assertEqual(self.main(*extra), 0)
                self.assertEqual(
                    self.order[:4],
                    [
                        ("sessions", MATCH),
                        *(("close", x) for x in self.MINE),
                        "run",
                    ],
                )
                self.assertEqual(
                    self.said()[n : n + 3],
                    [
                        f"run launch TASK-7 mode=new sid={SID}",
                        *(f"run tui-closed TASK-7 session={x}" for x in self.MINE),
                    ],
                )

    def test_an_unknown_listing_is_logged_and_the_run_goes_on(self):
        self.api.live = None
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.said()[1], "run tui-error TASK-7 msg=tmux sessions unknown")
        self.assertEqual(self.order[:2], [("sessions", MATCH), "run"])

    def test_a_close_error_is_logged_and_the_others_still_close(self):
        self.api.live = self.MINE
        self.api.errors = {self.MINE[0]: DriverError("kill failed")}
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said()[1:3],
            [
                f"run tui-error TASK-7 session={self.MINE[0]} msg=DriverError: kill failed",
                f"run tui-closed TASK-7 session={self.MINE[1]}",
            ],
        )

    def test_tui_runs_in_the_delegates_tui_mode_and_shows_its_session_once_begun(self):
        self.api.shown = "attach-command"
        self.done()
        extra = ("--tui", "--split=below", "--split-from=dev", "--opener=w0t0p0:ABC")
        self.assertEqual(self.main(*extra, "--events=/x/ev.log"), 0)
        _, spec = self.call()
        self.assertEqual(
            (spec.mode, spec.name, spec.events, spec.sid), ("tui", TUI, "/x/ev.log", SID)
        )
        self.assertEqual(
            [o for o in self.order if o[0] == "show"],
            [("show", TUI, "below", "dev", "w0t0p0:ABC", 2)],
        )
        self.assertEqual(self.err, "attach-command\n")
        self.assertIn(f"writeback step TASK-7 sid={SID} step=move:in_review", self.said())

    def test_a_show_error_is_logged_never_raised(self):
        self.api.shown = DriverError(f"no tmux session {TUI}")
        self.done()
        self.assertEqual(self.main("--tui"), 0)
        said = self.said()
        self.assertIn(
            f"run tui-error TASK-7 session={TUI} msg=DriverError: no tmux session {TUI}", said
        )
        self.assertEqual(
            said[-2:],
            [
                f"writeback step TASK-7 sid={SID} step=move:in_review",
                f"run end TASK-7 sid={SID} exit=0",
            ],
        )
        self.assertEqual(self.err, "")
