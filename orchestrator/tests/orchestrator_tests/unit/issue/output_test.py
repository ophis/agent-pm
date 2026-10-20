"""The issue command's output rules: a page fitted within the limit, a read cut to the default
limits, the archive dry run's plan and the archive refusal's text.
"""

import json

from agent_pm.orchestrator.engine.flow.types import ArchivePlan, ArchiveRefused
from agent_pm.orchestrator.issue import output
from agent_pm.orchestrator.trackers.linear import issue_archive, issue_read
from agent_pm.orchestrator.trackers.linear.linear_tracker import LinearTracker
from orchestrator_tests.case import Case
from orchestrator_tests.unit.issue_fixtures import (
    ArchiveGql,
    Gql,
    comment,
    conn,
    issue_node,
    rel,
    tree,
)


def relation(kind, field, ident):
    return {"type": kind, field: rel(ident)}


def many(n, prefix="SUB", kind="completed"):
    return [tree(f"{prefix}-{i}", kind) for i in range(1, n + 1)]


def archive(gql, ident="TASK-1"):
    return issue_archive.archive(LinearTracker(gql), gql, ident)


def dry_run(gql, ident="TASK-1"):
    return output.dry_run(issue_archive.plan(LinearTracker(gql), gql, ident))


def row(n, name="x", cut=()):
    return {"id": f"id-{n}", "name": name}, list(cut), f"cur-{n}"


class FitPage(Case):
    def test_within_the_limit_nothing_is_cut(self):
        page = json.loads(output.fit_page([row(1), row(2, cut=["team_ids"])], "end"))
        self.assertEqual(page["next_cursor"], "end")
        self.assertTrue(page["has_next_page"])
        self.assertEqual(page["truncated"], ["items.1.team_ids"])
        last = json.loads(output.fit_page([], None))
        self.assertEqual(
            last, {"items": [], "truncated": [], "has_next_page": False, "next_cursor": None}
        )

    def test_over_the_limit_trailing_items_drop_and_the_cursor_follows_the_last_kept(self):
        rows = [row(n, "名" * 400, cut=["team_ids"]) for n in range(50)]
        data = output.fit_page(rows, None)
        self.assertLessEqual(len(data), 8192)
        page = json.loads(data)
        kept = len(page["items"])
        self.assertLess(kept, 50)
        self.assertEqual((page["has_next_page"], page["next_cursor"]), (True, f"cur-{kept - 1}"))
        self.assertEqual(page["truncated"], [f"items.{n}.team_ids" for n in range(kept)])
        self.assertEqual(page["items"][-1]["name"], "名" * 400)

    def test_a_lone_item_shortens_its_text_but_keeps_its_references(self):
        item = {"id": "i" * 3000, "name": "n" * 9000, "labels": [{"id": "l1", "name": "m" * 3000}]}
        page = json.loads(output.fit_page([(item, [], "c")], None))
        got = page["items"][0]
        self.assertEqual((got["id"], got["labels"][0]["id"]), ("i" * 3000, "l1"))
        self.assertLess(len(got["name"]), 9000)
        self.assertTrue(("n" * 9000).startswith(got["name"]))
        self.assertIn("items.0.name", page["truncated"])
        self.assertIsNone(page["next_cursor"])

    def test_references_alone_over_the_limit_fail(self):
        item = {"id": "i" * 9000, "name": ""}
        with self.assertRaises(OverflowError):
            output.fit_page([(item, [], "c")], None)


class Compact(Case):
    def test_default_limits_mark_every_cut(self):
        node = issue_node(
            description="d" * 601,
            comments=conn([comment(n, body="b" * 301 if n == 6 else "ok") for n in range(1, 7)]),
            children=conn([rel(f"TASK-{n}") for n in range(10, 21)]),
            relations=conn(
                [relation("blocks", "relatedIssue", f"TASK-{n}") for n in range(30, 41)]
            ),
            inverseRelations=conn(
                [relation("blocks", "issue", f"TASK-{n}") for n in range(50, 61)]
            ),
        )
        view = output.compact(issue_read.view(Gql(node), "TASK-2"))
        self.assertEqual(len(view["description"]), 600)
        self.assertEqual([c["id"] for c in view["comments"]], ["c6", "c5", "c4", "c3", "c2"])
        self.assertEqual(len(view["comments"][0]["body"]), 300)
        for column in ("children", "blocks", "blocked_by"):
            self.assertEqual(len(view[column]), 10, column)
        self.assertEqual(
            view["truncated"],
            ["description", "comments", "comments.0.body", "blocks", "blocked_by", "children"],
        )

    def test_within_limits_nothing_is_cut(self):
        node = issue_node(
            description="d" * 600,
            comments=conn([comment(n, body="b" * 300) for n in range(1, 6)]),
            children=conn([rel(f"TASK-{n}") for n in range(10, 20)]),
        )
        view = output.compact(issue_read.view(Gql(node), "TASK-2"))
        self.assertEqual(view["truncated"], [])
        self.assertEqual(len(view["description"]), 600)
        self.assertEqual(len(view["comments"]), 5)

    def test_a_page_mark_is_not_repeated(self):
        node = issue_node(children=conn([rel(f"TASK-{n}") for n in range(10, 21)], more=True))
        view = output.compact(issue_read.view(Gql(node), "TASK-2"))
        self.assertEqual(view["truncated"], ["children"])


class Refusal(Case):
    def test_the_refusal_text_and_its_plan(self):
        gql = ArchiveGql(
            tree("TASK-1", "completed", tree("TASK-2", "canceled", tree("TASK-3", "started")))
        )
        with self.assertRaises(ArchiveRefused) as cm:
            archive(gql)
        self.assertEqual(
            output.refusal("TASK-1", cm.exception.plan),
            "archive TASK-1 refused, nothing archived: 1 unfinished of 3 (it and its unarchived "
            "sub-issues): TASK-3 (Started); complete or cancel each first "
            "(agent-pm-orchestrator issue archive TASK-1 --dry-run lists them)",
        )
        self.assertIsInstance(cm.exception.plan, ArchivePlan)
        self.assertEqual(cm.exception.plan.ident, "TASK-1")

    def test_a_long_refusal_names_the_first_ones_and_counts_the_rest(self):
        gql = ArchiveGql(tree("TASK-1", "completed", *many(300, kind="started")))
        with self.assertRaises(ArchiveRefused) as cm:
            archive(gql)
        text = output.refusal("TASK-1", cm.exception.plan)
        self.assertIn("300 unfinished", text)
        self.assertIn("SUB-1 (Started)", text)
        self.assertIn(f"and {300 - output.SHOWN} more", text)
        self.assertNotIn(f"SUB-{output.SHOWN + 1} ", text)


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
