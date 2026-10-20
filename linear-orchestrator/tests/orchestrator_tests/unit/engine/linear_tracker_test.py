"""The Linear adapter of the flow Tracker: prune's and the router's query text and its reads and writes."""

import ast
import hashlib
import inspect
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from agent_pm.linear_orchestrator.engine import linear_tracker, ops, prune, router
from agent_pm.linear_orchestrator.engine.linear_tracker import LinearTracker
from agent_pm.linear_orchestrator.engine.types import LinearError, Unavailable
from agent_pm.linear_orchestrator.flow.tracker import Tracker
from agent_pm.linear_orchestrator.flow.types import Finished, IssueState, Snapshot, SubIssue
from orchestrator_tests.case import Case
from orchestrator_tests.prune_fixtures import NOW, ROLES, TEAM_OBJ
from orchestrator_tests.unit.board_ids import DUPLICATE, STATES, TASK_GROUP, TEAM, team_node

# sha256 of each query's text on orchestrator-revamped: the fake Linear pairs queries by text
BASE = {
    "Q_ISSUE": "caaea6b489b3a2fc9db26b44693fd8762fe1284b0001b9e7cad1846bd7deae5c",
    "Q_FINISHED": "d3001511732429ed7b87bfac7cc78c579737f209e410fced49a1a9030ed0bc37",
    "Q_CHILDREN": "115b789a59aec88fa1686fa993764aacae8f6af3cf0b01ec900ee82603f526fa",
    "M_ARCHIVE": "86a7493d87b61ff2c087cf86df51894a9c04f95a6fa6d1d22be15947967ca1d3",
}
# the router's, on orchestrator-revamped; LISTS: q_issues(<fields>) by the fields' name
ROUTER = {
    "RELATIONS": "b443e58b5f277fc5edad3e11004a6af6f76beb96e1858ce1920077c9f20798a8",
    "Q_RELATIONS": "c271398e2c19c59eb040a3f5988e3ddf0d7787ae9dbd824e63541dd45a4afc53",
    "Q_RECHECK": "585cff398cb172d48816a2e46fe7fa611ed917ddae67fe1cf0678c9d42080572",
    "Q_HISTORY": "478618723212a6dfd1b6e0d47f80b592daf40d85cf8a06329e30c0c7c6cb58f7",
}
LISTS = {
    "": "3a5086418d895504931848eadf8243f03aad1cfc77f6391f1dc8ab357c034213",
    "RELATIONS": "7d8c26493df3c7696386e34534445288717b64ace14ed4866316e03bb51463fa",
}
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


def names():
    found = {getattr(linear_tracker, n): n for n in (*BASE, *ROUTER)}
    found |= {getattr(ops, n): n for n in ("Q_TEAM", "Q_TASK_GROUP", "Q_USER")}
    lists = linear_tracker.q_issues(), linear_tracker.q_issues(linear_tracker.RELATIONS)
    return found | dict(zip(lists, ("LIST", "LIST_RELATIONS"), strict=True))


class Gql:
    """Answers by query name from replies (a list is one reply per call); records every call."""

    NAMES = names()

    def __init__(self, **replies):
        self.replies, self.calls = replies, []

    def names(self):
        return [self.NAMES[q] for q, _ in self.calls]

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        name = self.NAMES[query]
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
        for name in BASE:
            with self.subTest(query=name):
                self.assertIs(getattr(prune, name), getattr(linear_tracker, name))

    def test_router_text_is_the_base_text(self):
        texts = {n: getattr(linear_tracker, n) for n in ROUTER}
        texts |= {f"q_issues({f})": linear_tracker.q_issues(texts.get(f, "")) for f in LISTS}
        digests = ROUTER | {f"q_issues({f})": d for f, d in LISTS.items()}
        for name, text in texts.items():
            with self.subTest(query=name):
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digests[name])

    def test_router_reexports_are_the_adapter_strings(self):
        for name in (*ROUTER, "q_issues"):
            with self.subTest(query=name):
                self.assertIs(getattr(router, name), getattr(linear_tracker, name))


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


def users(*ids):
    return [{"users": {"nodes": [{"id": i}] if i else []}} for i in ids]


def board_cfg():
    return SimpleNamespace(
        team=TEAM,
        states=STATES,
        task_label_group=TASK_GROUP,
        task_labels={"light-research": "label-1"},
        human_members=("human@x.test",),
        roles={"pm": SimpleNamespace(account="pm@agents.test")},
    )


def connected(**replies):
    group = {"issueLabel": {"isGroup": True, "children": {"nodes": [{"id": "label-1"}]}}}
    replies = {"Q_TEAM": {"teams": {"nodes": [team_node()]}}, "Q_TASK_GROUP": group} | replies
    return LinearTracker.connect(Gql(**replies), board_cfg(), say=None)


class Connect(Case):
    def test_reads_in_order(self):
        found = connected(Q_USER=users("u-human", "u-pm"))
        self.assertEqual(found.gql.names(), ["Q_TEAM", "Q_TASK_GROUP", "Q_USER", "Q_USER"])
        self.assertEqual(
            [v for _, v in found.gql.calls],
            [{"t": TEAM}, {"i": TASK_GROUP}, {"e": "human@x.test"}, {"e": "pm@agents.test"}],
        )
        self.assertEqual((found.team.id, found.team.states), (TEAM, STATES))
        self.assertEqual((found.roles, found.humans), ({"u-pm": "pm"}, frozenset({"u-human"})))

    def test_errors_propagate(self):
        for replies, message in (
            ({"Q_TEAM": {"teams": {"nodes": []}}}, f"team {TEAM} not found"),
            ({"Q_USER": users(None)}, "human_members not found in Linear: human@x.test"),
            ({"Q_USER": users("u-human", None)}, "account 'pm@agents.test' not found"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(LinearError, message):
                connected(**replies)


def listed(state):
    return {
        "team": {"id": {"eq": TEAM}},
        "project": {"null": False},
        "assignee": {"id": {"in": list(ROLES)}},
        "state": {"id": {"eq": STATES[state]}},
    }


class Assigned(Case):
    def test_filter_per_state(self):
        for state, key in ((IssueState.READY, "todo"), (IssueState.RUNNING, "in_progress")):
            with self.subTest(state=state):
                gql = Gql(LIST={"issues": {"nodes": [{"identifier": "TASK-1"}]}})
                self.assertEqual(tracker(gql).assigned(state), [{"identifier": "TASK-1"}])
                self.assertEqual(gql.calls, [(linear_tracker.q_issues(), {"f": listed(key)})])

    def test_other_state_value_error(self):
        for state in (IssueState.REVIEW, IssueState.APPROVED, IssueState.DONE):
            with self.subTest(state=state), self.assertRaises(ValueError):
                tracker(Gql()).assigned(state)


class Role(Case):
    def test_by_assignee_id(self):
        self.assertEqual(tracker(Gql()).role({"assignee": {"id": "u-pm"}}), "pm")


class Interface(Case):
    def test_linear_tracker_implements_every_method(self):
        self.assertEqual(
            Tracker.__abstractmethods__,
            {"read", "finished", "sub_issues", "archive", "role", "assigned"},
        )
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
