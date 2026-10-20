"""The Linear adapter of the flow Tracker: prune's query text and its reads and writes."""

import ast
import hashlib
import inspect
from dataclasses import replace
from datetime import timedelta

from agent_pm.linear_orchestrator.engine import linear_tracker, promote, prune
from agent_pm.linear_orchestrator.engine.linear_tracker import LinearTracker
from agent_pm.linear_orchestrator.engine.types import LinearError, Unavailable
from agent_pm.linear_orchestrator.flow.tracker import Tracker
from agent_pm.linear_orchestrator.flow.types import Finished, IssueState, Snapshot, SubIssue
from orchestrator_tests.case import Case
from orchestrator_tests.prune_fixtures import NOW, ROLES, TEAM_OBJ
from orchestrator_tests.unit.board_ids import DUPLICATE, STATES, TEAM

# sha256 of each query's text on orchestrator-revamped: the fake Linear pairs queries by text
BASE = {
    "Q_ISSUE": "caaea6b489b3a2fc9db26b44693fd8762fe1284b0001b9e7cad1846bd7deae5c",
    "Q_FINISHED": "d3001511732429ed7b87bfac7cc78c579737f209e410fced49a1a9030ed0bc37",
    "Q_CHILDREN": "115b789a59aec88fa1686fa993764aacae8f6af3cf0b01ec900ee82603f526fa",
    "M_ARCHIVE": "86a7493d87b61ff2c087cf86df51894a9c04f95a6fa6d1d22be15947967ca1d3",
    "Q_HANDOFF": "d5f85ca23fbe967486bc4ddfab203a172d029c0533575e9ea5d74a5a92e141b9",
    "Q_DETAIL": "b2e26faf4a8f91935fd12b5c84aa12b6bd23ed8781de3db8d13214e5cff469a5",
    "Q_CHILD": "4fa2b83d88ba5fc7dd9a33223140644eb8647521ac24c8c19ba044602dbca7a1",
    "M_CREATE": "a67ba5a98ce64a1689f6842fca53f944e76ab2437837d95c7c5ce1940911b430",
    "M_RELATE": "6672d8a892ce59d43f46f25990e6867358dff5cf5e8f79438d77d09bb1e587d5",
}
PRUNE = ("Q_ISSUE", "Q_FINISHED", "Q_CHILDREN", "M_ARCHIVE")
PROMOTE = ("Q_HANDOFF", "Q_DETAIL", "Q_CHILD", "M_CREATE", "M_RELATE")
BACKLOG = "00000000-0000-4000-8000-000000000019"


def ago(hours):
    return NOW - timedelta(hours=hours)


def moves(*pairs):
    """History nodes, newest first as Linear lists them: (hours ago, state id)."""
    return {"nodes": [{"createdAt": ago(h).isoformat(), "toStateId": s} for h, s in pairs]}


def issue(state, *pairs, canceled=None, completed=None):
    return {
        "state": {"id": state},
        "canceledAt": canceled and ago(canceled).isoformat(),
        "completedAt": completed and ago(completed).isoformat(),
        "history": moves(*pairs),
    }


def child(ident, kind):
    return {"id": f"id-{ident}", "identifier": ident, "state": {"type": kind}}


class Gql:
    """Answers by query text from replies (a list is one reply per call); records every call."""

    def __init__(self, **replies):
        self.replies, self.calls = replies, []

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        name = next(n for n in BASE if getattr(linear_tracker, n) == query)
        reply = self.replies[name]
        reply = reply.pop(0) if isinstance(reply, list) else reply
        if isinstance(reply, Exception):
            raise reply
        return reply


def tracker(gql):
    return LinearTracker(gql, TEAM_OBJ, ROLES)


class Queries(Case):
    def test_text_is_the_base_text(self):
        for name, digest in BASE.items():
            with self.subTest(query=name):
                text = getattr(linear_tracker, name)
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)

    def test_prune_reexports_are_the_adapter_strings(self):
        for name in PRUNE:
            with self.subTest(query=name):
                self.assertIs(getattr(prune, name), getattr(linear_tracker, name))

    def test_promote_reexports_are_the_adapter_strings(self):
        for name in PROMOTE:
            with self.subTest(query=name):
                self.assertIs(getattr(promote, name), getattr(linear_tracker, name))


class Read(Case):
    def read(self, found):
        gql = Gql(Q_ISSUE={"issue": found})
        snapshot = tracker(gql).read("TASK-1")
        self.assertEqual(gql.calls, [(linear_tracker.Q_ISSUE, {"i": "TASK-1"})])
        return snapshot

    def test_maps_each_configured_state(self):
        expected = {
            STATES["todo"]: IssueState.READY,
            STATES["in_progress"]: IssueState.RUNNING,
            STATES["in_review"]: IssueState.REVIEW,
            STATES["handoff"]: IssueState.APPROVED,
            STATES["done"]: IssueState.DONE,
            STATES["canceled"]: IssueState.CANCELED,
            DUPLICATE: IssueState.CANCELED,
        }
        for state, abstract in expected.items():
            with self.subTest(state=abstract, id=state):
                self.assertEqual(self.read(issue(state, (3, state))), Snapshot(abstract, ago(3)))

    def test_other_state_is_none(self):
        self.assertEqual(self.read(issue(BACKLOG, (5, BACKLOG))), Snapshot(None, ago(5)))

    def test_finished_since_is_latest_stamp_or_move(self):
        done, canceled = STATES["done"], STATES["canceled"]
        rows = (
            (issue(done, (9, canceled), completed=3), ago(3)),
            (issue(done, (1, done), (9, canceled), completed=3), ago(1)),
            (issue(DUPLICATE, (9, STATES["in_review"]), canceled=4), ago(4)),
            (issue(canceled, (9, STATES["in_review"])), None),
        )
        for found, since in rows:
            with self.subTest(since=since):
                self.assertEqual(self.read(found).since, since)

    def test_open_since_is_latest_move_into_its_state(self):
        review = STATES["in_review"]
        found = issue(review, (1, STATES["handoff"]), (4, review), (10, review))
        self.assertEqual(self.read(found), Snapshot(IssueState.REVIEW, ago(4)))
        self.assertEqual(self.read(issue(review)), Snapshot(IssueState.REVIEW, None))

    def test_a_shared_todo_or_in_progress_id_stays_ready_or_running(self):
        for key, active, abstract in (
            ("handoff", "todo", IssueState.READY),
            ("in_review", "in_progress", IssueState.RUNNING),
        ):
            with self.subTest(shared=(key, active)):
                team = replace(TEAM_OBJ, states={**TEAM_OBJ.states, key: STATES[active]})
                gql = Gql(Q_ISSUE={"issue": issue(STATES[active], (3, STATES[active]))})
                found = LinearTracker(gql, team, ROLES).read("TASK-1")
                self.assertEqual(found, Snapshot(abstract, ago(3)))

    def test_missing_issue_is_none(self):
        self.assertIsNone(self.read(None))

    def test_errors_propagate(self):
        for error in (
            LinearError("linear api error: boom"),
            Unavailable("issue", status=503),
            OSError("keychain"),
        ):
            with self.subTest(error=error), self.assertRaises(type(error)) as raised:
                tracker(Gql(Q_ISSUE=error)).read("TASK-1")
            self.assertIs(raised.exception, error)


class FinishedIssues(Case):
    def node(self, ident, *children, completed=2):
        return {
            "id": f"id-{ident}",
            "identifier": ident,
            "canceledAt": None,
            "completedAt": ago(completed).isoformat(),
            "children": {"nodes": list(children)},
            "history": moves(),
        }

    def page(self, nodes, cursor, *, more=True):
        return {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}

    def test_variables_and_paging(self):
        first, second = self.node("TASK-1", child("TASK-3", "started")), self.node("TASK-2")
        gql = Gql(Q_FINISHED=[self.page([first], "1"), self.page([second], "2", more=False)])
        found = tracker(gql).finished(("pm", "engineer"))
        common = {
            "t": TEAM,
            "d": [STATES["done"]],
            "s": sorted({STATES["canceled"], DUPLICATE}),
            "a": ["u-pm", "u-engineer"],
        }
        self.assertEqual(
            gql.calls,
            [
                (linear_tracker.Q_FINISHED, {**common, "c": None}),
                (linear_tracker.Q_FINISHED, {**common, "c": "1"}),
            ],
        )
        self.assertEqual(
            found,
            [
                Finished(
                    "id-TASK-1",
                    "TASK-1",
                    ago(2),
                    (SubIssue("id-TASK-3", "TASK-3", finished=False),),
                ),
                Finished("id-TASK-2", "TASK-2", ago(2), ()),
            ],
        )

    def test_stops_on_repeated_or_empty_cursor(self):
        for pages in (
            [self.page([self.node("TASK-1")], "1"), self.page([self.node("TASK-2")], "1")],
            [self.page([self.node("TASK-1")], None)],
            [self.page([self.node("TASK-1")], "")],
        ):
            with self.subTest(cursors=[p["issues"]["pageInfo"]["endCursor"] for p in pages]):
                gql = Gql(Q_FINISHED=list(pages))
                self.assertEqual(len(tracker(gql).finished(("pm",))), len(pages))
                self.assertEqual(len(gql.calls), len(pages))

    def test_owners_map_to_user_ids(self):
        for owners, ids in (
            (("pm", "engineer"), ["u-pm", "u-engineer"]),
            (("researcher",), ["u-researcher"]),
            ((), []),
        ):
            with self.subTest(owners=owners):
                gql = Gql(Q_FINISHED=self.page([], None, more=False))
                tracker(gql).finished(owners)
                self.assertEqual(gql.calls[0][1]["a"], ids)


class SubIssues(Case):
    def test_finished_by_state_type(self):
        kinds = {"completed": True, "canceled": True, "duplicate": True, "started": False}
        kinds |= {"unstarted": False, "backlog": False}
        nodes = [child(f"TASK-{i}", kind) for i, kind in enumerate(kinds, start=2)]
        gql = Gql(Q_CHILDREN={"issue": {"children": {"nodes": nodes}}})
        found = tracker(gql).sub_issues("id-TASK-1")
        self.assertEqual(gql.calls, [(linear_tracker.Q_CHILDREN, {"i": "id-TASK-1"})])
        self.assertEqual(
            found,
            tuple(
                SubIssue(f"id-TASK-{i}", f"TASK-{i}", done)
                for i, done in enumerate(kinds.values(), start=2)
            ),
        )


class Archive(Case):
    def test_sends_m_archive(self):
        gql = Gql(M_ARCHIVE={"issueArchive": {"success": True}})
        self.assertIsNone(tracker(gql).archive("id-TASK-1"))
        self.assertEqual(gql.calls, [(linear_tracker.M_ARCHIVE, {"i": "id-TASK-1"})])

    def test_success_false_raises(self):
        gql = Gql(M_ARCHIVE={"issueArchive": {"success": False}})
        with self.assertRaisesRegex(LinearError, "^issueArchive: success: false$"):
            tracker(gql).archive("id-TASK-1")


class Interface(Case):
    def test_linear_tracker_implements_every_method(self):
        self.assertEqual(Tracker.__abstractmethods__, {"read", "finished", "sub_issues", "archive"})
        self.assertIsInstance(LinearTracker(None, TEAM_OBJ, ROLES), Tracker)


class PruneSeam(Case):
    def test_prune_module_builds_no_graphql(self):
        tree = ast.parse(inspect.getsource(prune))
        texts = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        self.assertEqual([t for t in texts if "query(" in t or "mutation(" in t], [])
        self.assertEqual(
            [
                ast.unparse(n)
                for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and ast.unparse(n) in ("self.gql", "self.team")
            ],
            [],
        )
        from_ops = {
            a.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            and n.module == "agent_pm.linear_orchestrator.engine.ops"
            for a in n.names
        }
        self.assertEqual(from_ops, {"role_ids", "team"})
