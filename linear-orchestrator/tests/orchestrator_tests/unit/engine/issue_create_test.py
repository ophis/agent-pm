"""agent-pm-linear issue create: every reference resolved before the one create, only the given
fields set, Backlog by default, then each blocking relation; a failed relation reported with only
the commands that add it.
"""

from types import SimpleNamespace

from agent_pm.linear_orchestrator.engine import client, issue_create
from agent_pm.linear_orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LinearError,
    RelationsFailed,
    Unavailable,
    WriteUnknown,
)
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.issue_fixtures import (
    NEW,
    VIEWER,
    by_name,
    create_gql,
    edges,
    state,
    uid,
)

BODY = 'say "hi", `ls`, $HOME\n中文\n'


def args(**given):
    defaults = {
        "title": "Fix login",
        "team": None,
        "label": [],
        "project": None,
        "milestone": None,
        "parent": None,
        "assignee": None,
        "state": None,
        "blocks": [],
        "blocked_by": [],
    }
    return SimpleNamespace(**{**defaults, **given})


def run(fake, **given):
    return issue_create.create(fake, *issue_create.plan(fake, args(**given), TEAM, BODY))


def relation(blocker, blocked):
    return {"in": {"type": "blocks", "issueId": blocker, "relatedIssueId": blocked}}


class Create(Case):
    def test_every_field_and_both_blocking_directions(self):
        fake = create_gql()
        receipt = run(
            fake,
            label=["bug", "ops"],
            project="Agent PM",
            milestone="M1",
            parent="TASK-1",
            assignee="Ann",
            state="Todo",
            blocks=["TASK-3"],
            blocked_by=["TASK-4"],
        )
        created, *related = fake.writes
        self.assertEqual(
            created[1]["in"],
            {
                "teamId": TEAM,
                "title": "Fix login",
                "description": BODY,
                "stateId": uid(3),
                "labelIds": [uid(4), uid(5)],
                "projectId": uid(7),
                "projectMilestoneId": uid(8),
                "parentId": "id-TASK-1",
                "assigneeId": uid(9),
            },
        )
        self.assertEqual(
            [r[1] for r in related],
            [relation("id-TASK-9", "id-TASK-3"), relation("id-TASK-4", "id-TASK-9")],
        )
        self.assertEqual(
            receipt, {"issue": NEW, "operation": "create", "result": "changed", "actor": VIEWER}
        )

    def test_unset_fields_stay_unset_and_the_state_is_backlog(self):
        fake = create_gql()
        run(fake)
        self.assertEqual(
            fake.writes[0][1]["in"],
            {"teamId": TEAM, "title": "Fix login", "description": BODY, "stateId": uid(2)},
        )
        self.assertEqual(len(fake.writes), 1)

    def test_a_given_team_is_resolved(self):
        fake = create_gql(teams=edges([{"id": uid(1), "name": "Ops", "key": "OPS"}]))
        run(fake, team="ops")
        self.assertEqual(fake.writes[0][1]["in"]["teamId"], uid(1))
        self.assertEqual(
            fake.sent("workflowStates")[0]["f"]["and"][0], {"team": {"id": {"eq": uid(1)}}}
        )

    def test_no_backlog_state_is_refused_never_another(self):
        fake = create_gql(workflowStates=by_name(state(3, "Todo")))
        with self.assertRaises(IssueError) as cm:
            run(fake)
        self.assertIn("Backlog", str(cm.exception))
        self.assertEqual(fake.writes, [])

    def test_any_unresolved_reference_creates_nothing(self):
        cases = (
            {"label": ["bug", "nope"]},
            {"label": ["old"]},
            {"project": "Nope"},
            {"milestone": "M1"},
            {"project": "Agent PM", "milestone": "M9"},
            {"parent": "NOPE-1"},
            {"parent": "not an id"},
            {"assignee": "Bob"},
            {"state": "backlog"},
            {"blocks": ["TASK-3", "NOPE-2"]},
            {"blocked_by": ["NOPE-3"]},
        )
        for given in cases:
            fake = create_gql()
            with self.subTest(given=given), self.assertRaises(IssueError):
                run(fake, **given)
            self.assertEqual(fake.writes, [])

    def test_repeated_labels_and_dependencies_count_once(self):
        fake = create_gql()
        run(fake, label=["bug", "bug"], blocks=["TASK-3", "TASK-3"], blocked_by=["TASK-4"] * 2)
        created, *related = fake.writes
        self.assertEqual(created[1]["in"]["labelIds"], [uid(4)])
        self.assertEqual(
            [r[1] for r in related],
            [relation("id-TASK-9", "id-TASK-3"), relation("id-TASK-4", "id-TASK-9")],
        )

    def test_one_issue_in_both_directions_is_refused(self):
        fake = create_gql()
        with self.assertRaises(IssueError) as cm:
            run(fake, blocks=["TASK-3"], blocked_by=["TASK-3"])
        self.assertIn("TASK-3", str(cm.exception))
        self.assertIn("--blocks and --blocked-by", str(cm.exception))
        self.assertEqual(fake.writes, [])

    def test_a_failed_relation_reports_only_what_to_add(self):
        fake = create_gql(
            relations=[
                {"success": True},
                GraphQLErrors([{"message": "rate limited"}]),
                Unavailable("issueRelationCreate", reason="timed out"),
            ]
        )
        with self.assertRaises(RelationsFailed) as cm:
            run(fake, blocks=["TASK-3", "TASK-5"], blocked_by=["TASK-4"])
        report = cm.exception.report
        self.assertEqual(
            (report["issue"], report["operation"], report["result"]), (NEW, "create", "partial")
        )
        self.assertEqual(report["done"], [{"blocks": "TASK-3"}])
        self.assertEqual(
            [{k: f[k] for k in ("blocks", "blocked_by") if k in f} for f in report["failed"]],
            [{"blocks": "TASK-5"}, {"blocked_by": "TASK-4"}],
        )
        self.assertIn("rate limited", report["failed"][0]["error"])
        self.assertIn("timed out", report["failed"][1]["error"])
        self.assertEqual(
            report["recover"],
            [
                "agent-pm-linear issue relation add TASK-9 --blocks TASK-5",
                "agent-pm-linear issue relation add TASK-9 --blocked-by TASK-4",
            ],
        )
        text = str(cm.exception)
        for phrase in ("TASK-9", NEW["url"], "2 of 3", "relation add", "not run create again"):
            self.assertIn(phrase, text)
        self.assertEqual(
            [client.operation(q) for q, _ in fake.writes],
            ["issueCreate"] + ["issueRelationCreate"] * 3,
        )

    def test_a_create_outage_is_result_unknown_and_never_retried(self):
        fake = create_gql(created=Unavailable("issueCreate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            run(fake, blocks=["TASK-3"])
        text = str(cm.exception)
        for phrase in ("result unknown", "timed out", f"list --team {TEAM} --query='Fix login'"):
            self.assertIn(phrase, text)
        self.assertEqual(len(fake.writes), 1)

    def test_the_outage_check_names_the_given_team_and_survives_a_dash_title(self):
        fake = create_gql(
            created=Unavailable("issueCreate", reason="timed out"),
            teams=edges([{"id": uid(1), "name": "Ops", "key": "OPS"}]),
        )
        with self.assertRaises(WriteUnknown) as cm:
            run(fake, team="OPS", title="-x")
        self.assertIn(f"issue list --team {uid(1)} --query=-x ", str(cm.exception))

    def test_a_refused_create_fails_and_adds_no_relation(self):
        for created in ({"success": False, "issue": None}, GraphQLErrors([{"message": "boom"}])):
            fake = create_gql(created=created)
            with self.subTest(created=created), self.assertRaises(LinearError) as cm:
                run(fake, blocks=["TASK-3"])
            self.assertNotIsInstance(cm.exception, WriteUnknown)
            self.assertEqual(len(fake.writes), 1)
