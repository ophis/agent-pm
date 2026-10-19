"""agent-pm-linear issue move: the state resolved in the issue's own team, one write, the receipt."""

from agent_pm.linear_orchestrator.engine import issue_write, ops
from agent_pm.linear_orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LinearError,
    Unavailable,
    WriteUnknown,
)
from orchestrator_tests.case import Case
from orchestrator_tests.unit.issue_fixtures import VIEWER, WriteGql, conn, team, write_issue

DONE = (("s-todo", "Todo", "unstarted"), ("s-done", "Done", "completed"))


class Move(Case):
    def test_a_name_resolves_within_the_issue_team(self):
        # both teams name a state Done; each issue moves to its own team's
        for ident, want in (("TASK-2", "s-task-done"), ("OPS-3", "s-ops-done")):
            key = ident.split("-")[0]
            states = (("s-todo", "Todo", "unstarted"), (want, "Done", "completed"))
            gql = WriteGql(write_issue(ident, team_node=team(key, states)))
            with self.subTest(ident=ident):
                receipt = issue_write.move(gql, ident, "Done")
                self.assertEqual(gql.writes, [(ops.M_STATE, {"i": f"id-{ident}", "s": want})])
                self.assertEqual(
                    receipt,
                    {
                        "issue": {
                            "id": f"id-{ident}",
                            "identifier": ident,
                            "url": f"https://linear.app/t/issue/{ident}",
                        },
                        "operation": "move",
                        "result": "changed",
                        "actor": VIEWER,
                    },
                )

    def test_a_state_id_of_the_issue_team_resolves(self):
        gql = WriteGql(write_issue(team_node=team("TASK", DONE)))
        self.assertEqual(issue_write.move(gql, "TASK-2", "s-done")["result"], "changed")
        self.assertEqual(gql.writes, [(ops.M_STATE, {"i": "id-TASK-2", "s": "s-done"})])

    def test_already_in_the_state_is_unchanged_and_writes_nothing(self):
        for value in ("Done", "s-done"):
            gql = WriteGql(write_issue(state="s-done", team_node=team("TASK", DONE)))
            with self.subTest(value=value):
                receipt = issue_write.move(gql, "TASK-2", value)
                self.assertEqual((receipt["result"], gql.writes), ("unchanged", []))
                self.assertEqual(receipt["actor"], VIEWER)

    def test_an_unresolved_state_writes_nothing_and_lists_the_team_states(self):
        cases = (
            ("Shipped", "not a state", ("Todo (s-todo)", "Done (s-done)")),
            ("done", "not a state", ("Done (s-done)",)),
            ("completed", "a state type", ("Done (s-done)",)),
            ("s-elsewhere", "not a state", ("Todo (s-todo)",)),
        )
        for value, reason, candidates in cases:
            gql = WriteGql(write_issue(team_node=team("TASK", DONE)))
            with self.subTest(value=value), self.assertRaises(IssueError) as cm:
                issue_write.move(gql, "TASK-2", value)
            text = str(cm.exception)
            self.assertIn(f"{value!r}", text)
            self.assertIn(reason, text)
            self.assertIn("team TASK", text)
            for candidate in candidates:
                self.assertIn(candidate, text)
            self.assertEqual(gql.writes, [])

    def test_a_name_twice_in_the_team_is_ambiguous(self):
        states = (("s-a", "Done", "completed"), ("s-b", "Done", "canceled"))
        gql = WriteGql(write_issue(team_node=team("TASK", states)))
        with self.assertRaises(IssueError) as cm:
            issue_write.move(gql, "TASK-2", "Done")
        self.assertIn("ambiguous", str(cm.exception))
        self.assertIn("Done (s-a)", str(cm.exception))
        self.assertIn("Done (s-b)", str(cm.exception))
        self.assertEqual(gql.writes, [])

    def test_later_state_pages_are_read(self):
        first = team("TASK", (("s-todo", "Todo", "unstarted"),), more=True, cursor="p-1")
        later = conn([{"id": "s-done", "name": "Done", "type": "completed"}])
        gql = WriteGql(write_issue(team_node=first), pages=[later])
        self.assertEqual(issue_write.move(gql, "TASK-2", "Done")["result"], "changed")
        self.assertIn((issue_write.Q_STATES, {"t": "t-TASK", "c": "p-1"}), gql.calls)
        self.assertEqual(gql.writes, [(ops.M_STATE, {"i": "id-TASK-2", "s": "s-done"})])

    def test_no_such_issue_writes_nothing(self):
        gql = WriteGql(None)
        with self.assertRaises(IssueError) as cm:
            issue_write.move(gql, "TASK-2", "Done")
        self.assertIn("no such issue", str(cm.exception))
        self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_never_retried(self):
        gql = WriteGql(
            write_issue(team_node=team("TASK", DONE)),
            result=Unavailable("issueUpdate", reason="timed out"),
        )
        with self.assertRaises(WriteUnknown) as cm:
            issue_write.move(gql, "TASK-2", "Done")
        text = str(cm.exception)
        for phrase in ("move TASK-2", "result unknown", "timed out", "issue read TASK-2"):
            self.assertIn(phrase, text)
        self.assertEqual(len(gql.writes), 1)

    def test_a_refused_write_fails_and_is_not_unknown(self):
        for result in ({"success": False}, GraphQLErrors([{"message": "boom"}])):
            gql = WriteGql(write_issue(team_node=team("TASK", DONE)), result=result)
            with self.subTest(result=result), self.assertRaises(LinearError) as cm:
                issue_write.move(gql, "TASK-2", "Done")
            self.assertNotIsInstance(cm.exception, WriteUnknown)
            self.assertEqual(len(gql.writes), 1)
