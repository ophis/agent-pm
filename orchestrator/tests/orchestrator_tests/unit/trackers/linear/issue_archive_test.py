"""agent-pm-linear issue archive: every unarchived descendant read over every page at every level,
the archive only when all are finished, nothing written after a failed read.
"""

from agent_pm.orchestrator.trackers.linear import issue_archive, linear_tracker
from agent_pm.orchestrator.trackers.linear.types import (
    ArchiveRefused,
    GraphQLErrors,
    LinearError,
    Unavailable,
    WriteUnknown,
)
from orchestrator_tests.case import Case
from orchestrator_tests.unit.issue_fixtures import VIEWER, ArchiveGql, tree


def many(n, prefix="SUB", kind="completed"):
    return [tree(f"{prefix}-{i}", kind) for i in range(1, n + 1)]


class StuckGql(ArchiveGql):
    """Every children page claims another after it, at cursor (None: no cursor)."""

    def __init__(self, root, cursor):
        super().__init__(root)
        self.cursor = cursor

    def _page(self, node, offset):
        page = super()._page(node, 0)
        page["pageInfo"] = {"hasNextPage": True, "endCursor": self.cursor}
        return page


class Archive(Case):
    def test_all_finished_archives_once_and_prints_the_receipt(self):
        root = tree("TASK-1", "completed", tree("TASK-2", "canceled", tree("TASK-3")))
        gql = ArchiveGql(root)
        self.assertEqual(
            issue_archive.archive(gql, "TASK-1"),
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

    def test_a_duplicate_is_finished_as_prune_counts_it(self):
        for name, root in (
            ("issue", tree("TASK-1", "duplicate")),
            ("sub-issue", tree("TASK-1", "completed", tree("TASK-2", "duplicate"))),
        ):
            with self.subTest(duplicate=name):
                plan = issue_archive.dry_run(ArchiveGql(root), "TASK-1")
                self.assertEqual((plan["result"], plan["unfinished"]), ("changed", []))
                gql = ArchiveGql(root)
                self.assertEqual(issue_archive.archive(gql, "TASK-1")["result"], "changed")
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
                issue_archive.archive(gql, "TASK-1")
            self.assertIn(unfinished, str(cm.exception))
            self.assertIn("agent-pm-linear issue archive TASK-1 --dry-run", str(cm.exception))
            self.assertEqual(gql.writes, [])

    def test_every_children_page_is_read_at_every_level(self):
        root = tree(
            "TASK-1", "completed", *many(300), tree("TASK-2", "completed", *many(260, "GC"))
        )
        gql = ArchiveGql(root)
        self.assertEqual(issue_archive.archive(gql, "TASK-1")["result"], "changed")
        self.assertIn((issue_archive.Q_CHILDREN, {"i": "id-TASK-1", "c": "250"}), gql.calls)
        self.assertIn((issue_archive.Q_CHILDREN, {"i": "id-TASK-2", "c": None}), gql.calls)
        self.assertIn((issue_archive.Q_CHILDREN, {"i": "id-TASK-2", "c": "250"}), gql.calls)

    def test_an_archived_issue_is_unchanged_without_reading_sub_issues(self):
        gql = ArchiveGql(
            tree("TASK-1", "started", tree("TASK-2", "started"), archived="2026-10-01")
        )
        receipt = issue_archive.archive(gql, "TASK-1")
        self.assertEqual((receipt["result"], receipt["actor"]), ("unchanged", VIEWER))
        self.assertEqual([q for q, _ in gql.calls], [issue_archive.Q_ARCHIVE])

    def test_a_failed_read_writes_nothing(self):
        root = tree("TASK-1", "completed", *many(300), tree("TASK-2", "completed", tree("TASK-3")))
        cases = (
            (("id-TASK-1", 250), LinearError("linear api error: boom"), LinearError),
            (("id-TASK-1", 250), GraphQLErrors([{"message": "boom"}]), GraphQLErrors),
            (("id-TASK-2", 0), Unavailable("issue", reason="timed out"), Unavailable),
            (("id-TASK-2", 0), None, LinearError),
            (("id-TASK-1", 0), None, LinearError),
        )
        for page, error, raised in cases:
            gql = ArchiveGql(root, broken={page: error})
            with self.subTest(page=page, error=error), self.assertRaises(raised):
                issue_archive.archive(gql, "TASK-1")
            self.assertEqual(gql.writes, [])

    def test_a_next_page_without_a_new_cursor_fails_and_writes_nothing(self):
        for cursor in (None, "0"):
            gql = StuckGql(tree("TASK-1", "completed", *many(2)), cursor)
            with self.subTest(cursor=cursor), self.assertRaisesRegex(LinearError, "endCursor"):
                issue_archive.archive(gql, "TASK-1")
            self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown_and_success_false_fails(self):
        for result, raised, phrase in (
            (Unavailable("issueArchive", reason="timed out"), WriteUnknown, "result unknown"),
            ({"success": False}, LinearError, "success: false"),
        ):
            gql = ArchiveGql(tree("TASK-1"), result=result)
            with self.subTest(phrase=phrase), self.assertRaises(raised) as cm:
                issue_archive.archive(gql, "TASK-1")
            self.assertIn(phrase, str(cm.exception))
            self.assertEqual(len(gql.writes), 1)

    def test_a_long_refusal_names_the_first_ones_and_counts_the_rest(self):
        gql = ArchiveGql(tree("TASK-1", "completed", *many(300, kind="started")))
        with self.assertRaises(ArchiveRefused) as cm:
            issue_archive.archive(gql, "TASK-1")
        text = str(cm.exception)
        self.assertIn("300 unfinished", text)
        self.assertIn("SUB-1 (Started)", text)
        self.assertIn(f"and {300 - issue_archive.SHOWN} more", text)
        self.assertNotIn(f"SUB-{issue_archive.SHOWN + 1} ", text)


class DryRun(Case):
    def test_lists_every_descendant_and_writes_nothing(self):
        root = tree("TASK-1", "completed", *many(251), tree("TASK-2", "canceled", tree("TASK-3")))
        gql = ArchiveGql(root)
        plan = issue_archive.dry_run(gql, "TASK-1")
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
        plan = issue_archive.dry_run(ArchiveGql(root), "TASK-1")
        self.assertEqual((plan["result"], plan["unfinished_count"]), ("refused", 2))
        self.assertEqual([u["identifier"] for u in plan["unfinished"]], ["TASK-1", "TASK-9"])

    def test_an_archived_issue_is_unchanged(self):
        plan = issue_archive.dry_run(ArchiveGql(tree("TASK-1", archived="2026-10-01")), "TASK-1")
        self.assertEqual((plan["result"], plan["descendants"]), ("unchanged", []))
