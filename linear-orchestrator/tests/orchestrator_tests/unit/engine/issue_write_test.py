"""agent-pm-linear issue move and comment: the state resolved in the issue's own team, the body read
verbatim, one write, the receipt.
"""

import io
import os

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
BODY = 'say "hi", `ls`, $HOME and ${x} \\n\nline two\n中文正文\n'


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

    def test_a_next_states_page_without_a_new_cursor_fails_and_writes_nothing(self):
        todo = (("s-todo", "Todo", "unstarted"),)
        for first, pages in (
            (team("TASK", todo, more=True), []),
            (team("TASK", todo, more=True, cursor="p-1"), [conn([], more=True, cursor="p-1")]),
        ):
            gql = WriteGql(write_issue(team_node=first), pages=pages)
            with self.subTest(pages=pages), self.assertRaisesRegex(LinearError, "endCursor"):
                issue_write.move(gql, "TASK-2", "Done")
            self.assertEqual(gql.writes, [])

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


class ReadBody(Case):
    def test_a_file_or_stdin_reads_verbatim(self):
        path = os.path.join(self.home, "body.md")
        with open(path, "wb") as f:
            f.write(BODY.encode("utf-8"))
        self.assertEqual(issue_write.read_body(path, io.BytesIO()), BODY)
        self.assertEqual(issue_write.read_body("-", io.BytesIO(BODY.encode("utf-8"))), BODY)

    def test_an_unusable_body_is_refused(self):
        blank, latin = os.path.join(self.home, "blank.md"), os.path.join(self.home, "latin.md")
        with open(blank, "w", encoding="utf-8") as f:
            f.write(" \n\t\n")
        with open(latin, "wb") as f:
            f.write("café".encode("latin-1"))
        cases = (
            (os.path.join(self.home, "missing.md"), b"", "No such file"),
            (self.home, b"", "directory"),
            (latin, b"", "not UTF-8"),
            (blank, b"", "empty"),
            ("-", b"", "empty"),
            ("-", b"\xff", "not UTF-8"),
        )
        for path, stdin, phrase in cases:
            with self.subTest(phrase=phrase, path=path), self.assertRaises(IssueError) as cm:
                issue_write.read_body(path, io.BytesIO(stdin))
            self.assertIn(f"--body-file {path}", str(cm.exception))
            self.assertIn(phrase, str(cm.exception))


class Comment(Case):
    def test_posts_the_body_verbatim_as_a_variable(self):
        gql = WriteGql(write_issue())
        receipt = issue_write.comment(gql, "TASK-2", BODY)
        self.assertEqual(gql.writes, [(issue_write.M_COMMENT, {"i": "id-TASK-2", "b": BODY})])
        self.assertEqual(
            receipt,
            {
                "issue": {
                    "id": "id-TASK-2",
                    "identifier": "TASK-2",
                    "url": "https://linear.app/t/issue/TASK-2",
                },
                "operation": "comment",
                "result": "changed",
                "actor": VIEWER,
                "comment_id": "c-new",
            },
        )

    def test_no_such_issue_posts_nothing(self):
        gql = WriteGql(None)
        with self.assertRaises(IssueError):
            issue_write.comment(gql, "TASK-2", BODY)
        self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_never_retried(self):
        gql = WriteGql(write_issue(), result=Unavailable("commentCreate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            issue_write.comment(gql, "TASK-2", BODY)
        self.assertIn("comment TASK-2: result unknown", str(cm.exception))
        self.assertNotIn("中文", str(cm.exception))
        self.assertEqual(len(gql.writes), 1)

    def test_a_refused_write_fails_and_is_not_unknown(self):
        for result in ({"success": False}, GraphQLErrors([{"message": "boom"}])):
            gql = WriteGql(write_issue(), result=result)
            with self.subTest(result=result), self.assertRaises(LinearError) as cm:
                issue_write.comment(gql, "TASK-2", BODY)
            self.assertNotIsInstance(cm.exception, WriteUnknown)
            self.assertEqual(len(gql.writes), 1)
