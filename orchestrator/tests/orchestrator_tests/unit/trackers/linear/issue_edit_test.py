"""agent-pm-orchestrator issue edit: one issueUpdate naming only what changes; a task or client
replaces the issue's label of that group, every other label staying; nothing changed writes
nothing.
"""

from agent_pm.orchestrator.engine.flow.types import Priority
from agent_pm.orchestrator.engine.types import IssueError, Unavailable, WriteUnknown
from agent_pm.orchestrator.issue.output import receipt_payload
from agent_pm.orchestrator.trackers.linear import issue_edit, issue_label, issue_read
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import TASK_GROUP
from orchestrator_tests.unit.issue_fixtures import (
    CLIENT_GROUP,
    CLIENT_LABELS,
    OTHER_TEAM,
    TASK_LABELS,
    VIEWER,
    EditGql,
    issue_change,
    label,
    names_cfg,
)

BUILD, LIGHT = label(0xB1, "build", group=TASK_GROUP), label(0xB2, "light", group=TASK_GROUP)
CODEX, CLAUDE = label(0xC1, "a", group=CLIENT_GROUP), label(0xC2, "b", group=CLIENT_GROUP)
KEEP = label(3, "keep")
CATALOG = (BUILD, LIGHT, CODEX, CLAUDE, KEEP)
BODY = 'say "hi", `ls`, $HOME\n中文\n'


def edit(gql, body=None, cfg=None, **given):
    change = issue_change(**{"ident": "TASK-2", **given, "body": body})
    return issue_edit.edit(gql, cfg or names_cfg(), change)


class Edit(Case):
    def test_each_field_alone_is_the_only_one_written(self):
        for given, body, want in (
            ({"title": "New"}, None, {"title": "New"}),
            ({}, BODY, {"description": BODY}),
            ({"priority": Priority.HIGH}, None, {"priority": 2}),
            ({"priority": Priority.NONE}, None, {"priority": 0}),
        ):
            gql = EditGql(CATALOG, [KEEP["id"]], fields={"priority": 3})
            with self.subTest(given=given, body=body):
                receipt = edit(gql, body, **given)
                self.assertEqual(gql.writes, [(issue_edit.M_EDIT, {"i": "id-TASK-2", "in": want})])
                self.assertEqual(
                    receipt_payload(receipt),
                    {
                        "issue": {
                            "id": "id-TASK-2",
                            "identifier": "TASK-2",
                            "url": "https://linear.app/t/issue/TASK-2",
                        },
                        "operation": "edit",
                        "result": "changed",
                        "actor": VIEWER,
                    },
                )

    def test_every_field_at_once_is_one_write(self):
        gql = EditGql(CATALOG, [BUILD["id"], CODEX["id"], KEEP["id"]])
        edit(gql, BODY, title="New", task="light-build", client="claude", priority=Priority.LOW)
        self.assertEqual(
            gql.writes,
            [
                (
                    issue_edit.M_EDIT,
                    {
                        "i": "id-TASK-2",
                        "in": {
                            "title": "New",
                            "description": BODY,
                            "priority": 4,
                            "addedLabelIds": [TASK_LABELS["light-build"], CLIENT_LABELS["claude"]],
                            "removedLabelIds": [BUILD["id"], CODEX["id"]],
                        },
                    },
                )
            ],
        )

    def test_a_task_or_client_replaces_its_group_label_and_keeps_every_other(self):
        for given, added, gone in (
            ({"task": "light-build"}, TASK_LABELS["light-build"], BUILD["id"]),
            ({"client": "claude"}, CLIENT_LABELS["claude"], CODEX["id"]),
        ):
            gql = EditGql(CATALOG, [BUILD["id"], CODEX["id"], KEEP["id"]])
            with self.subTest(given=given):
                edit(gql, **given)
                (_, variables), *_ = gql.writes
                self.assertEqual(
                    variables["in"], {"addedLabelIds": [added], "removedLabelIds": [gone]}
                )
                kept = {BUILD["id"], CODEX["id"], KEEP["id"], added} - {gone}
                self.assertEqual(set(gql.carried), kept)

    def test_a_task_on_an_issue_without_one_only_adds(self):
        gql = EditGql(CATALOG, [KEEP["id"]])
        edit(gql, task="build")
        self.assertEqual(gql.writes[0][1]["in"], {"addedLabelIds": [TASK_LABELS["build"]]})

    def test_labels_on_later_pages_are_read(self):
        extra = [label(0x1000 + n, f"x{n}") for n in range(issue_read.PAGE)]
        gql = EditGql((*CATALOG, *extra), [n["id"] for n in extra] + [BUILD["id"]])
        edit(gql, task="light-build")
        self.assertIn(
            (issue_label.Q_MORE, {"i": "id-TASK-2", "c": str(issue_read.PAGE)}), gql.calls
        )
        self.assertEqual(gql.writes[0][1]["in"]["removedLabelIds"], [BUILD["id"]])

    def test_every_given_value_already_so_writes_nothing(self):
        gql = EditGql(
            CATALOG,
            [BUILD["id"], CODEX["id"]],
            fields={"title": "Same", "description": BODY, "priority": 2},
        )
        receipt = edit(
            gql, BODY, title="Same", task="build", client="codex", priority=Priority.HIGH
        )
        self.assertEqual((receipt.result, receipt.operation, gql.writes), ("unchanged", "edit", []))
        self.assertEqual(receipt_payload(receipt)["actor"], VIEWER)

    def test_an_unconfigured_name_is_refused_before_any_read(self):
        for given in ({"task": "nope"}, {"client": "nope"}):
            gql = EditGql(CATALOG, [])
            with self.subTest(given=given), self.assertRaises(IssueError):
                edit(gql, **given)
            self.assertEqual(gql.calls, [])

    def test_a_task_or_client_on_another_team_writes_nothing(self):
        other = {"team": {"id": OTHER_TEAM, "key": "OPS"}}
        for given in ({"task": "build"}, {"client": "codex", "title": "New"}):
            gql = EditGql(CATALOG, [], fields=other)
            with self.subTest(given=given), self.assertRaises(IssueError) as cm:
                edit(gql, **given)
            self.assertIn("is in team OPS", str(cm.exception))
            self.assertEqual(gql.writes, [])

    def test_title_description_and_priority_need_no_team(self):
        gql = EditGql(CATALOG, [], fields={"team": {"id": OTHER_TEAM, "key": "OPS"}})
        self.assertEqual(edit(gql, title="New").result, "changed")

    def test_no_such_issue_writes_nothing(self):
        gql = EditGql(CATALOG, [])
        with self.assertRaises(IssueError) as cm:
            edit(gql, ident="TASK-3", title="New")
        self.assertIn("no such issue", str(cm.exception))
        self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown(self):
        gql = EditGql(CATALOG, [], result=Unavailable("issueUpdate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            edit(gql, title="New")
        text = str(cm.exception)
        for phrase in ("edit TASK-2", "result unknown", "timed out", "issue read TASK-2"):
            self.assertIn(phrase, text)
        self.assertEqual(len(gql.writes), 1)
