"""agent-pm-orchestrator issue archive: every unarchived descendant read over every page at every level,
the archive only when all are finished, nothing written after a failed read.
"""

from agent_pm.orchestrator.engine.flow.types import ArchivePlan, ArchiveRefused
from agent_pm.orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LinearError,
    Unavailable,
    WriteUnknown,
)
from agent_pm.orchestrator.trackers.linear import issue_archive, linear_tracker
from agent_pm.orchestrator.trackers.linear.linear_tracker import LinearTracker
from orchestrator_tests.case import Case
from orchestrator_tests.unit.issue_fixtures import VIEWER, ArchiveGql, tree


def many(n, prefix="SUB", kind="completed"):
    return [tree(f"{prefix}-{i}", kind) for i in range(1, n + 1)]


def archive(gql, ident="TASK-1"):
    return issue_archive.archive(LinearTracker(gql), gql, ident)


def dry_run(gql, ident="TASK-1"):
    return issue_archive.dry_run(LinearTracker(gql), gql, ident)


class StuckGql(ArchiveGql):
    """Every children page claims another after it, at cursor (None: no cursor)."""

    def __init__(self, root, cursor):
        super().__init__(root)
        self.cursor = cursor

    def _page(self, node, offset):
        page = super()._page(node, 0)
        page["pageInfo"] = {"hasNextPage": True, "endCursor": self.cursor}
        return page


class GoneGql(ArchiveGql):
    """The issue the lookup found is gone when the archive read comes."""

    def _plan(self, key):
        return {"issue": None}


class Archive(Case):
    def test_all_finished_archives_once_and_prints_the_receipt(self):
        root = tree("TASK-1", "completed", tree("TASK-2", "canceled", tree("TASK-3")))
        gql = ArchiveGql(root)
        self.assertEqual(
            archive(gql),
            {
                "issue": {
                    "id": "id-TASK-1",
                    "identifier": "TASK-1",
                    "url": "https://linear.app/t/issue/TASK-1",
                },
                "operation": "archive",
                "result": "changed",
                "actor": VIEWER,
            },
        )
        self.assertEqual(gql.writes, [(linear_tracker.M_ARCHIVE, {"i": "id-TASK-1"})])
        self.assertEqual(gql.calls[0], (issue_archive.Q_ISSUE, {"i": "TASK-1"}))

    def test_a_duplicate_is_finished_as_prune_counts_it(self):
        for name, root in (
            ("issue", tree("TASK-1", "duplicate")),
            ("sub-issue", tree("TASK-1", "completed", tree("TASK-2", "duplicate"))),
        ):
            with self.subTest(duplicate=name):
                plan = dry_run(ArchiveGql(root))
                self.assertEqual((plan["result"], plan["unfinished"]), ("changed", []))
                gql = ArchiveGql(root)
                self.assertEqual(archive(gql)["result"], "changed")
                self.assertEqual(gql.writes, [(linear_tracker.M_ARCHIVE, {"i": "id-TASK-1"})])

    def test_any_unfinished_issue_refuses_and_writes_nothing(self):
        cases = {
            "root": (tree("TASK-1", "started", tree("TASK-2")), "TASK-1"),
            "grandchild": (
                tree("TASK-1", "completed", tree("TASK-2", "completed", tree("TASK-3", "backlog"))),
                "TASK-3",
            ),
            "251st child": (
                tree("TASK-1", "completed", *many(250), tree("TASK-9", "unstarted")),
                "TASK-9",
            ),
            "251st grandchild": (
                tree(
                    "TASK-1",
                    "completed",
                    tree("TASK-2", "completed", *many(250), tree("TASK-9", "triage")),
                ),
                "TASK-9",
            ),
        }
        for name, (root, unfinished) in cases.items():
            gql = ArchiveGql(root)
            with self.subTest(name=name), self.assertRaises(ArchiveRefused) as cm:
                archive(gql)
            self.assertIn(unfinished, str(cm.exception))
            self.assertIn(unfinished, [s.ident for s in cm.exception.plan.unfinished])
            self.assertIn("agent-pm-orchestrator issue archive TASK-1 --dry-run", str(cm.exception))
            self.assertEqual(gql.writes, [])

    def test_the_refusal_text_and_its_plan(self):
        gql = ArchiveGql(
            tree("TASK-1", "completed", tree("TASK-2", "canceled", tree("TASK-3", "started")))
        )
        with self.assertRaises(ArchiveRefused) as cm:
            archive(gql)
        self.assertEqual(
            str(cm.exception),
            "archive TASK-1 refused, nothing archived: 1 unfinished of 3 (it and its unarchived "
            "sub-issues): TASK-3 (Started); complete or cancel each first "
            "(agent-pm-orchestrator issue archive TASK-1 --dry-run lists them)",
        )
        self.assertIsInstance(cm.exception.plan, ArchivePlan)
        self.assertEqual(cm.exception.plan.ident, "TASK-1")

    def test_every_children_page_is_read_at_every_level(self):
        root = tree(
            "TASK-1", "completed", *many(300), tree("TASK-2", "completed", *many(260, "GC"))
        )
        gql = ArchiveGql(root)
        self.assertEqual(archive(gql)["result"], "changed")
        self.assertIn((linear_tracker.Q_CHILDREN, {"i": "id-TASK-1", "c": "300"}), gql.calls)
        self.assertIn((linear_tracker.Q_CHILDREN, {"i": "id-TASK-2", "c": None}), gql.calls)
        self.assertIn((linear_tracker.Q_CHILDREN, {"i": "id-TASK-2", "c": "250"}), gql.calls)

    def test_an_archived_issue_is_unchanged_without_reading_sub_issues(self):
        gql = ArchiveGql(
            tree("TASK-1", "started", tree("TASK-2", "started"), archived="2026-10-01")
        )
        receipt = archive(gql)
        self.assertEqual((receipt["result"], receipt["actor"]), ("unchanged", VIEWER))
        self.assertEqual(
            [q for q, _ in gql.calls], [issue_archive.Q_ISSUE, linear_tracker.Q_ARCHIVE]
        )

    def test_a_missing_or_malformed_ident_is_an_issue_error_before_any_read(self):
        for gql, ident in ((ArchiveGql(None), "TASK-1"), (ArchiveGql(tree()), "task 1")):
            with self.subTest(ident=ident), self.assertRaises(IssueError):
                archive(gql, ident)
            self.assertNotIn(linear_tracker.Q_ARCHIVE, [q for q, _ in gql.calls])

    def test_an_issue_gone_before_the_archive_read_writes_nothing(self):
        gql = GoneGql(tree())
        with self.assertRaisesRegex(LinearError, "^id-TASK-1: issue not found$"):
            archive(gql)
        self.assertEqual(gql.writes, [])

    def test_a_failed_read_writes_nothing(self):
        root = tree("TASK-1", "completed", *many(300), tree("TASK-2", "completed", tree("TASK-3")))
        cases = (
            (("id-TASK-1", 50), LinearError("linear api error: boom"), LinearError),
            (("id-TASK-1", 250), GraphQLErrors([{"message": "boom"}]), GraphQLErrors),
            (("id-TASK-2", 0), Unavailable("issue", reason="timed out"), Unavailable),
            (("id-TASK-2", 0), Unavailable("issueArchive", reason="timed out"), Unavailable),
            (("id-TASK-2", 0), None, LinearError),
            (("id-TASK-1", 0), None, LinearError),
        )
        for page, error, raised in cases:
            gql = ArchiveGql(root, broken={page: error})
            with self.subTest(page=page, error=error), self.assertRaises(raised):
                archive(gql)
            self.assertEqual(gql.writes, [])

    def test_a_next_page_without_a_new_cursor_fails_and_writes_nothing(self):
        for cursor in (None, "0"):
            gql = StuckGql(tree("TASK-1", "completed", *many(2)), cursor)
            with self.subTest(cursor=cursor), self.assertRaisesRegex(LinearError, "endCursor"):
                archive(gql)
            self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_success_false_fails(self):
        for result, raised, phrase in (
            (
                Unavailable("issueArchive", reason="timed out"),
                WriteUnknown,
                "archive TASK-1: result unknown (issueArchive: timed out): check with "
                "agent-pm-orchestrator issue read TASK-1 before retrying",
            ),
            (
                Unavailable("renamedArchive", reason="timed out"),
                WriteUnknown,
                "archive TASK-1: result unknown (renamedArchive: timed out)",
            ),
            ({"success": False}, LinearError, "success: false"),
        ):
            gql = ArchiveGql(tree("TASK-1"), result=result)
            with self.subTest(phrase=phrase), self.assertRaises(raised) as cm:
                archive(gql)
            self.assertIn(phrase, str(cm.exception))
            self.assertEqual(len(gql.writes), 1)

    def test_a_long_refusal_names_the_first_ones_and_counts_the_rest(self):
        gql = ArchiveGql(tree("TASK-1", "completed", *many(300, kind="started")))
        with self.assertRaises(ArchiveRefused) as cm:
            archive(gql)
        text = str(cm.exception)
        self.assertIn("300 unfinished", text)
        self.assertIn("SUB-1 (Started)", text)
        self.assertIn(f"and {300 - issue_archive.SHOWN} more", text)
        self.assertNotIn(f"SUB-{issue_archive.SHOWN + 1} ", text)


class DryRun(Case):
    def test_the_plan_as_json(self):
        root = tree("TASK-1", "started", tree("TASK-2", "canceled", tree("TASK-3", "unstarted")))
        self.assertEqual(
            dry_run(ArchiveGql(root)),
            {
                "issue": {
                    "id": "id-TASK-1",
                    "identifier": "TASK-1",
                    "url": "https://linear.app/t/issue/TASK-1",
                },
                "operation": "archive",
                "dry_run": True,
                "result": "refused",
                "descendant_count": 2,
                "descendants": [
                    {"id": "id-TASK-2", "identifier": "TASK-2", "state": "Canceled"},
                    {"id": "id-TASK-3", "identifier": "TASK-3", "state": "Unstarted"},
                ],
                "unfinished_count": 2,
                "unfinished": [
                    {"id": "id-TASK-1", "identifier": "TASK-1", "state": "Started"},
                    {"id": "id-TASK-3", "identifier": "TASK-3", "state": "Unstarted"},
                ],
                "truncated": [],
            },
        )

    def test_lists_every_descendant_and_writes_nothing(self):
        root = tree("TASK-1", "completed", *many(251), tree("TASK-2", "canceled", tree("TASK-3")))
        gql = ArchiveGql(root)
        plan = dry_run(gql)
        self.assertEqual(gql.writes, [])
        self.assertEqual(
            (plan["operation"], plan["dry_run"], plan["result"]), ("archive", True, "changed")
        )
        self.assertEqual(plan["descendant_count"], 253)
        self.assertEqual(
            [d["identifier"] for d in plan["descendants"]][-3:], ["SUB-251", "TASK-2", "TASK-3"]
        )
        self.assertEqual(
            plan["descendants"][-1],
            {"id": "id-TASK-3", "identifier": "TASK-3", "state": "Completed"},
        )
        self.assertEqual((plan["unfinished_count"], plan["unfinished"]), (0, []))

    def test_names_what_would_refuse(self):
        root = tree("TASK-1", "started", *many(250), tree("TASK-9", "unstarted"))
        plan = dry_run(ArchiveGql(root))
        self.assertEqual((plan["result"], plan["unfinished_count"]), ("refused", 2))
        self.assertEqual([u["identifier"] for u in plan["unfinished"]], ["TASK-1", "TASK-9"])

    def test_an_archived_issue_is_unchanged(self):
        plan = dry_run(ArchiveGql(tree("TASK-1", "started", archived="2026-10-01")))
        self.assertEqual(
            (plan["result"], plan["descendants"], plan["unfinished"]), ("unchanged", [], [])
        )
