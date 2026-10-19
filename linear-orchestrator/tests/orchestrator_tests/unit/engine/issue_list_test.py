"""agent-pm-linear issue list through main: one team's issues, every filter at once, updated first,
archived only on request, each item only its fields.
"""

import json
import os

from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.issue_fixtures import (
    OTHER_TEAM,
    IssueCase,
    Routes,
    edges,
    issue_row,
    label,
    project,
    state,
    team_ref,
    uid,
    user,
)

FIELDS = ["id", "identifier", "title", "url", "state", "labels", "assignee", "project", "parent_id"]


def of_team(nodes):
    """An issues route answering only the nodes of the filter's team, as Linear would."""

    def answer(variables):
        match = variables["f"]
        scope = (match["and"][0] if "and" in match else match)["team"]["id"]["eq"]
        return edges([n for n in nodes if n["team"]["id"] == scope])

    return answer


class Filters(IssueCase):
    def run_list(self, argv, gql):
        code, out, err = self.run_main(["list", *argv], gql)
        self.assertEqual((code, err), (0, ""), err)
        self.assertLessEqual(len(out), 8192)
        return json.loads(out)

    def test_only_the_configured_team_by_default_updated_first_without_archived(self):
        nodes = [issue_row("TASK-1"), issue_row("OPS-1", OTHER_TEAM, title="title TASK-1")]
        gql = Routes(issues=of_team(nodes))
        page = self.run_list([], gql)
        self.assertEqual([i["identifier"] for i in page["items"]], ["TASK-1"])
        query, variables = gql.calls[0]
        self.assertIn("orderBy: updatedAt", query)
        want = {"first": 20, "after": None, "f": {"team": {"id": {"eq": TEAM}}}, "a": False}
        self.assertEqual(variables, {**want, "service": "linear-api-key"})

    def test_another_team_by_key(self):
        nodes = [issue_row("TASK-1"), issue_row("OPS-1", OTHER_TEAM)]
        gql = Routes(teams=edges([{**team_ref(0, "OPS"), "id": OTHER_TEAM}]), issues=of_team(nodes))
        page = self.run_list(["--team", "ops"], gql)
        self.assertEqual([i["identifier"] for i in page["items"]], ["OPS-1"])

    def test_every_filter_holds_at_once(self):
        gql = Routes(
            workflowStates=edges([state(3, "Todo")]),
            issueLabels=edges([label(4, "bug")]),
            team={"projects": edges([project(5, "Agent PM")])},
            viewer=user(6, "Harness"),
            issue={"id": uid(7), "identifier": "TASK-7"},
            issues=edges([]),
        )
        argv = ["--query", "lookup", "--state", "Todo", "--label", "bug", "--project", "Agent PM"]
        page = self.run_list([*argv, "--assignee", "me", "--parent", "TASK-7"], gql)
        self.assertEqual(
            page, {"items": [], "has_next_page": False, "next_cursor": None, "truncated": []}
        )
        text = {"containsIgnoreCase": "lookup"}
        want = {
            "and": [
                {"team": {"id": {"eq": TEAM}}},
                {"or": [{"title": text}, {"description": text}]},
                {"state": {"id": {"eq": uid(3)}}},
                {"labels": {"some": {"id": {"eq": uid(4)}}}},
                {"project": {"id": {"eq": uid(5)}}},
                {"assignee": {"id": {"eq": uid(6)}}},
                {"parent": {"id": {"eq": uid(7)}}},
            ]
        }
        self.assertEqual(gql.sent("issues")[0]["f"], want)

    def test_archived_issues_only_when_asked(self):
        gql = Routes(issues=edges([issue_row("TASK-1")]))
        self.run_list(["--include-archived"], gql)
        self.assertTrue(gql.sent("issues")[0]["a"])

    def test_items_hold_only_their_fields(self):
        node = issue_row(
            "TASK-2",
            assignee={"id": uid(6), "name": "Ann"},
            project={"id": uid(5), "name": "Agent PM"},
            parent={"id": uid(9)},
            labels={"nodes": [{"id": uid(4), "name": "bug"}], "pageInfo": {"hasNextPage": True}},
        )
        page = self.run_list([], Routes(issues=edges([node])))
        item = page["items"][0]
        self.assertEqual(list(item), FIELDS)
        self.assertEqual(
            (item["state"], item["labels"], item["assignee"], item["project"], item["parent_id"]),
            (
                {"id": uid(3), "name": "Todo"},
                [{"id": uid(4), "name": "bug"}],
                {"id": uid(6), "name": "Ann"},
                {"id": uid(5), "name": "Agent PM"},
                uid(9),
            ),
        )
        self.assertEqual(page["truncated"], ["items.0.labels"])

    def test_a_page_over_the_limit_keeps_ids_and_continues(self):
        nodes = [issue_row(f"TASK-{n}", title="长" * 300) for n in range(50)]
        gql = Routes(issues=edges(nodes))
        page = self.run_list(["--limit", "50"], gql)
        kept = page["items"]
        self.assertLess(len(kept), 50)
        self.assertEqual(page["next_cursor"], f"cur-{kept[-1]['id']}")
        self.assertTrue(page["has_next_page"])
        self.assertEqual(kept[0]["url"], "https://linear.app/t/issue/TASK-0")

    def test_full_writes_every_matching_issue(self):
        gql = Routes(
            issues=[
                edges([issue_row("TASK-1")], more=True, cursor="c1"),
                edges([issue_row("TASK-2")]),
            ]
        )
        path = os.path.join(self.root, "issues.json")
        code, out, _ = self.run_main(["list", "--full", "--output", path], gql)
        self.assertEqual((code, json.loads(out)), (0, {"output": path}))
        with open(path, encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual([i["identifier"] for i in items], ["TASK-1", "TASK-2"])
        self.assertEqual([v["after"] for v in gql.sent("issues")], [None, "c1"])


class Unresolved(IssueCase):
    def test_an_unresolved_filter_exits_two_before_listing(self):
        cases = (
            (["--state", "Nope"], Routes(workflowStates=[edges([]), edges([])]), "lookup states"),
            (
                ["--label", "Type"],
                Routes(issueLabels=edges([label(4, "Type", is_group=True)])),
                "label group",
            ),
            (
                ["--assignee", "Ann"],
                Routes(users=edges([user(1, "Ann"), user(2, "Ann")])),
                "2 match",
            ),
            (["--parent", "nope"], Routes(), "want an issue identifier"),
            (["--team", "X"], Routes(teams=[edges([]), edges([])]), "lookup teams --query X"),
        )
        for argv, gql, phrase in cases:
            with self.subTest(argv=argv):
                code, out, err = self.run_main(["list", *argv], gql)
                self.assertEqual((code, out), (2, b""))
                self.assertIn(phrase, err)
                self.assertEqual(gql.sent("issues"), [])
