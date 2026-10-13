from agent_pm.linear_orchestrator.engine.types import LinearError, Stopped
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.writeback.bounce_fixtures import Bounce as BounceBase
from orchestrator_test_helpers.unit.writeback.data import (
    HANDOFF,
    HUMANS,
    ID,
    SRC_UUID,
    STATES,
    comment,
    issue,
    move,
    reread,
    sub,
)
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql


class Bounce1(BounceBase):
    def test_handoff_path(self):
        gql = Gql(issue=self.src(), states=self.DONE)
        reason = "ophis/x: no push permission"
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), reason)
        t = self.text(reason)
        self.assertEqual(
            gql.calls,
            [
                ("id", {"i": "PRD-3"}),
                ("unarchive", {"i": SRC_UUID}),
                sub(HUMANS[0], SRC_UUID),
                sub(HUMANS[1], SRC_UUID),
                comment(t, SRC_UUID),
                reread(SRC_UUID),
                move("in_review", SRC_UUID),
                comment(t),
                reread(),
                move("canceled"),
            ],
        )
        self.assertEqual(self.lines(), [])

    def test_skipped_moves_logged(self):
        gql = Gql(issue=self.src(), states=(STATES["handoff"], STATES["todo"]))
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        t = self.text("r")
        self.assertEqual(
            gql.calls[-4:], [comment(t, SRC_UUID), reread(SRC_UUID), comment(t), reread()]
        )
        self.assertEqual(
            self.lines(),
            [
                f"skip PRD-3 reason=move to in_review: issue is {STATES['handoff']}",
                f"skip {ID} reason=move to canceled: issue is {STATES['todo']}",
            ],
        )

    def test_mapped_reason_names_project_repos(self):
        gql = Gql(issue=self.src())
        reason = "project mapping ophis/x: not found or no access (HTTP 404)"
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), reason)
        self.assertIn(
            comment(
                self.text(
                    reason,
                    " Or fix ~/.agent-pm/config.local.toml's [project_repos] entry.",
                )
            ),
            gql.calls,
        )

    def test_unarchive_failure_ignored(self):
        for err in (LinearError("linear api error: not archived"), False):
            with self.subTest(err=err):
                gql = Gql(
                    issue=self.src(),
                    states=self.DONE,
                    fail=lambda name, _v: err if name == "unarchive" else None,
                )
                writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
                self.assertEqual(gql.calls[-1], move("canceled"))

    def test_subscribe_failure_noted_move_made(self):
        gql = Gql(
            issue=self.src(),
            states=self.DONE,
            fail=lambda name, v: (
                LinearError("linear api error: boom")
                if name == "subscribe" and v["e"] == HUMANS[1]
                else None
            ),
        )
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        t = self.text("r")
        self.assertIn(
            comment(
                t + "\n\nCould not subscribe bob@example.com: LinearError: linear api error: boom",
                SRC_UUID,
            ),
            gql.calls,
        )
        self.assertEqual(
            gql.calls[-5:],
            [reread(SRC_UUID), move("in_review", SRC_UUID), comment(t), reread(), move("canceled")],
        )

    def test_question_path(self):
        reason = "unreadable Repo line: 'nope'"
        for name, description, src, lookup in (
            ("not a handoff", "Repo: nope\n\nBuild it.", None, []),
            (
                "source in another team",
                HANDOFF,
                self.src(team="00000000-0000-4000-8000-0000000000ff"),
                [("id", {"i": "PRD-3"})],
            ),
            ("source missing", HANDOFF, None, [("id", {"i": "PRD-3"})]),
        ):
            with self.subTest(name):
                gql = Gql(issue=src)
                writeback.bounce(self.ctx(gql=gql), issue(description), reason)
                self.assertEqual(
                    gql.calls,
                    [
                        *lookup,
                        sub(HUMANS[0]),
                        sub(HUMANS[1]),
                        comment(self.question(reason)),
                        reread(),
                        move("in_review"),
                    ],
                )

    def test_question_subscribe_failure_noted(self):
        gql = Gql(
            fail=lambda name, v: False if name == "subscribe" and v["e"] == HUMANS[0] else None
        )
        writeback.bounce(self.ctx(gql=gql), issue("x"), "r")
        self.assertEqual(
            gql.calls[-3:],
            [
                comment(
                    self.question("r") + "\n\nCould not subscribe ann@example.com: "
                    "LinearError: issueSubscribe: success: false"
                ),
                reread(),
                move("in_review"),
            ],
        )

    def test_signal_exit_raises(self):
        for name in ("unarchive", "subscribe"):
            with self.subTest(name=name):
                gql = Gql(
                    issue=self.src(), fail=lambda n, _v: Stopped(129) if n == name else None
                )
                with self.assertRaises(Stopped):
                    writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
                self.assertEqual(gql.calls[-1][0], name)

    def test_other_failures_raise(self):
        gql = Gql(issue=self.src(), fail=lambda name, _v: False if name == "comment" else None)
        with self.assertRaises(LinearError):
            writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        self.assertEqual(gql.calls[-1], comment(self.text("r"), SRC_UUID))
        gql = Gql(
            fail=lambda name, _v: LinearError("linear api error: x") if name == "state" else None
        )
        with self.assertRaises(LinearError):
            writeback.bounce(self.ctx(gql=gql), issue("x"), "r")
