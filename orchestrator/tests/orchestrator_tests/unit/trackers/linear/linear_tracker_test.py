"""The Linear adapter of the flow Tracker: prune's, promote's, the router's and write-back's query
text and its reads and writes."""

import ast
import hashlib
import inspect
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from agent_pm.orchestrator.engine import (
    gate,
    inputs,
    issues,
    linear_run,
    promote,
    prune,
    related,
    router,
    run_comments,
    runs,
    target,
    writeback,
)
from agent_pm.orchestrator.engine.flow.tracker import Tracker
from agent_pm.orchestrator.engine.flow.types import (
    UNREADABLE,
    Approved,
    Comment,
    Detail,
    Finished,
    Found,
    Group,
    Handoff,
    Issue,
    IssueState,
    Link,
    Linked,
    Move,
    Note,
    Pick,
    Recheck,
    Ref,
    Snapshot,
    SubIssue,
)
from agent_pm.orchestrator.engine.types import IssueError, LinearError, Unavailable
from agent_pm.orchestrator.trackers.linear import (
    linear_tracker,
    ops,
)
from agent_pm.orchestrator.trackers.linear.linear_tracker import LinearTracker
from agent_pm.utils import one_line
from orchestrator_tests.case import Case
from orchestrator_tests.prune_fixtures import NOW, ROLES, TEAM_OBJ
from orchestrator_tests.unit.board_ids import DUPLICATE, STATES, TASK_GROUP, TEAM, team_node

# sha256 of each query's text on orchestrator-revamped: the fake Linear pairs queries by text
BASE = {
    "Q_ISSUE": "caaea6b489b3a2fc9db26b44693fd8762fe1284b0001b9e7cad1846bd7deae5c",
    "Q_FINISHED": "d3001511732429ed7b87bfac7cc78c579737f209e410fced49a1a9030ed0bc37",
    "Q_CHILDREN": "115b789a59aec88fa1686fa993764aacae8f6af3cf0b01ec900ee82603f526fa",
    "M_ARCHIVE": "86a7493d87b61ff2c087cf86df51894a9c04f95a6fa6d1d22be15947967ca1d3",
    "M_TITLE": "1b74f5b2bb7775a1147c8a6bac7f50c09a196de9122da2c52e7cd64eaf45d7be",
    "M_ATTACH": "8db2cfd3ed91f6b4daf1aeed80183275d14953ff02982847658243be789af1f4",
    "M_UNARCHIVE": "e0c0d3bff0bf11847fc7ffd4b98030ac98c9319cde37a09fe0745906d545d9a3",
    "Q_ATTACHED": "63dbe2b07f16ec03a33efb47cf14ddb32a3bac690ae5a62ac57c43c57fbe3c96",
    "Q_ID": "339eb6885dd45c62cfae3c2f1d2bbdcdf6995a2e008079832d405f3019f5adf9",
    "M_UPDATE": "95eaa4a0cccf407f93c0137e32a54edfd12fcc68a35f4c94b2ad407ad59377dd",
    "Q_HANDOFF": "d5f85ca23fbe967486bc4ddfab203a172d029c0533575e9ea5d74a5a92e141b9",
    "Q_DETAIL": "b2e26faf4a8f91935fd12b5c84aa12b6bd23ed8781de3db8d13214e5cff469a5",
    "Q_CHILD": "4fa2b83d88ba5fc7dd9a33223140644eb8647521ac24c8c19ba044602dbca7a1",
    "M_CREATE": "a67ba5a98ce64a1689f6842fca53f944e76ab2437837d95c7c5ce1940911b430",
    "M_RELATE": "6672d8a892ce59d43f46f25990e6867358dff5cf5e8f79438d77d09bb1e587d5",
    "Q_READ": "a94647460768537b760c88ed1ac21bbfb27f3bcd1f6c52280e6a04fd90a03173",
    "Q_SEARCH": "bda2e9a29af014b08f88961462a759452ec79f9c0a411933db1ec264595256ca",
    "Q_SEARCH_IDS": "054d541ce9250a347a2c489c69ed5c6e7580f1244c43b79d71c01bad0cc741e0",
    "Q_IDS": "fd0f74d3cdd0d0a34ba9f12805652c7ecedebd4d75e003bece27d4d9912f9b95",
    "Q_UPSTREAM": "be140943c548c5d9b8dc0940cd9dd8c712469da812be4997e0d9ebc3fad38eda",
}
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
REF = (SID, "session")
CID = ops.client_id(SID, "session")
NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")
# the router's, on orchestrator-revamped; LISTS: q_issues(<fields>) by the fields' name
ROUTER = {
    "RELATIONS": "b443e58b5f277fc5edad3e11004a6af6f76beb96e1858ce1920077c9f20798a8",
    "Q_RELATIONS": "c271398e2c19c59eb040a3f5988e3ddf0d7787ae9dbd824e63541dd45a4afc53",
    "Q_RECHECK": "585cff398cb172d48816a2e46fe7fa611ed917ddae67fe1cf0678c9d42080572",
    "Q_HISTORY": "478618723212a6dfd1b6e0d47f80b592daf40d85cf8a06329e30c0c7c6cb58f7",
}
LISTS = {
    "": "497ec35294c8fc0894d43e9f3925801b8dcfe6af7a961e18b39f92e03fdec959",
    "RELATIONS": "730acfcbed9ef3680074d0746fd8811b4f55621654e2be30148a7921dfcaabae",
}
BACKLOG = "00000000-0000-4000-8000-000000000019"
# ops queries the adapter delegates to
OPS = {getattr(ops, n): n for n in ("M_COMMENT", "M_STATE", "M_SUBSCRIBE", "Q_ISSUE_STATE")}
HUMANS = ("Me@X.com", "you@x.com")
RULES = (
    gate,
    inputs,
    issues,
    linear_run,
    promote,
    prune,
    related,
    router,
    run_comments,
    runs,
    target,
    writeback,
)


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
    found |= {getattr(ops, n): n for n in ("Q_TEAM", "Q_TASK_GROUP", "Q_USER")} | OPS
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


def tracker(gql, team=TEAM_OBJ):
    return LinearTracker(
        gql,
        states=team.states,
        team_id=team.id,
        finished=team.finished,
        roles=ROLES,
        humans=HUMANS,
    )


class Queries(Case):
    def test_text_is_the_base_text(self):
        for name, digest in BASE.items():
            with self.subTest(query=name):
                text = getattr(linear_tracker, name)
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)

    def test_router_text_is_the_base_text(self):
        texts = {n: getattr(linear_tracker, n) for n in ROUTER}
        texts |= {f"q_issues({f})": linear_tracker.q_issues(texts.get(f, "")) for f in LISTS}
        digests = ROUTER | {f"q_issues({f})": d for f, d in LISTS.items()}
        for name, text in texts.items():
            with self.subTest(query=name):
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digests[name])

    def test_lists_select_no_updated_at(self):
        for fields in ("", linear_tracker.RELATIONS):
            with self.subTest(fields=fields):
                self.assertNotIn("updatedAt", linear_tracker.q_issues(fields))


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
                found = tracker(gql, team).read("TASK-1")
                self.assertEqual(found, Snapshot(abstract, ago(3)))

    def test_without_finished_raises(self):
        gql = Gql()
        with self.assertRaisesRegex(TypeError, "^LinearTracker needs finished$"):
            LinearTracker(gql, states=TEAM_OBJ.states).read("TASK-1")
        self.assertEqual(gql.calls, [])

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

    def test_without_team_or_roles_raises(self):
        for kw, missing in (
            ({"finished": TEAM_OBJ.finished, "roles": ROLES}, "team_id"),
            ({"team_id": TEAM, "finished": TEAM_OBJ.finished}, "roles"),
            ({"team_id": TEAM, "roles": ROLES}, "finished"),
        ):
            with self.subTest(missing=missing):
                gql = Gql()
                with self.assertRaisesRegex(TypeError, f"^LinearTracker needs {missing}$"):
                    LinearTracker(gql, states=TEAM_OBJ.states, **kw).finished(("pm",))
                self.assertEqual(gql.calls, [])

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


class Texts:
    """Answers by query text from replies (a list is one reply per call); records every call."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        reply = self.replies[query]
        reply = reply.pop(0) if isinstance(reply, list) else reply
        if isinstance(reply, Exception):
            raise reply
        return reply


class Comments(Case):
    def test_put_comment_updates_then_creates(self):
        update = (linear_tracker.M_UPDATE, {"c": CID, "b": "text"})
        create = (ops.M_COMMENT_ID, {"c": CID, "i": "TASK-7", "b": "text"})
        for missing, calls in (
            ({"commentUpdate": {"success": True}}, [update]),
            ({"commentUpdate": {"success": False}}, [update]),
            (NOT_FOUND, [update, create]),
            ({"commentUpdate": None}, [update, create]),
        ):
            with self.subTest(update=missing):
                gql = Texts(
                    {
                        linear_tracker.M_UPDATE: missing,
                        ops.M_COMMENT_ID: {"commentCreate": {"success": True}},
                    }
                )
                ok = LinearTracker(gql).put_comment("TASK-7", "text", REF)
                self.assertEqual(gql.calls, calls)
                self.assertIs(ok, missing != {"commentUpdate": {"success": False}})

    def test_put_comment_reraises_other_errors(self):
        for error in (LinearError("linear api error: forbidden"), Unavailable("x", status=503)):
            with self.subTest(error=error), self.assertRaises(type(error)) as raised:
                LinearTracker(Texts({linear_tracker.M_UPDATE: error})).put_comment(
                    "TASK-7", "text", REF
                )
            self.assertIs(raised.exception, error)

    def comment(self, issue="id-TASK-7", *, mine=True):
        return {"comment": {"id": CID, "body": "b", "issue": {"id": issue}, "user": {"isMe": mine}}}

    def test_find_comment_by_ref(self):
        for reply, found in ((self.comment(), "b"), (NOT_FOUND, None), ({"comment": None}, None)):
            with self.subTest(found=found):
                gql = Texts({ops.Q_COMMENT: reply})
                self.assertEqual(LinearTracker(gql).find_comment("id-TASK-7", REF), found)
                self.assertEqual(gql.calls, [(ops.Q_COMMENT, {"c": CID})])

    def test_find_comment_not_ours_raises(self):
        for reply in (self.comment(issue="id-TASK-8"), self.comment(mine=False)):
            with self.subTest(reply=reply), self.assertRaisesRegex(LinearError, "is not ours"):
                LinearTracker(Texts({ops.Q_COMMENT: reply})).find_comment("id-TASK-7", REF)


class Account(Case):
    def calls(self, **kw):
        seen = []

        def gql(query, **v):
            seen.append((query, v))
            return {"comment": None}

        LinearTracker(gql, **kw).find_comment("id-TASK-7", REF)
        return seen

    def test_service_and_timeout_bind_each_call(self):
        self.assertEqual(
            self.calls(service="linear-api-key", timeout=10),
            [(ops.Q_COMMENT, {"service": "linear-api-key", "timeout": 10, "c": CID})],
        )
        self.assertEqual(
            self.calls(service="linear-api-key-engineer"),
            [(ops.Q_COMMENT, {"service": "linear-api-key-engineer", "c": CID})],
        )

    def test_no_service_calls_gql_as_given(self):
        self.assertEqual(self.calls(), [(ops.Q_COMMENT, {"c": CID})])


class Moves(Case):
    def move(self, now, to, frm=None):
        gql = Texts(
            {
                ops.Q_ISSUE_STATE: {"issue": {"state": {"id": STATES[now]}}},
                ops.M_STATE: {"issueUpdate": {"success": True}},
            }
        )
        left = LinearTracker(gql, states=STATES).move("id-TASK-1", to, frm=frm)
        return left, [(q, v.get("s")) for q, v in gql.calls]

    def test_guarded_move_moves_from_frm_only(self):
        read = (ops.Q_ISSUE_STATE, None)
        self.assertEqual(
            self.move("in_progress", IssueState.REVIEW, IssueState.RUNNING),
            (None, [read, (ops.M_STATE, STATES["in_review"])]),
        )
        self.assertEqual(
            self.move("handoff", IssueState.REVIEW, IssueState.RUNNING),
            (STATES["handoff"], [read]),
        )
        self.assertEqual(
            self.move("done", IssueState.CANCELED, IssueState.RUNNING), (STATES["done"], [read])
        )

    def test_guarded_move_already_in_to_returns_its_state(self):
        self.assertEqual(
            self.move("in_review", IssueState.REVIEW, IssueState.RUNNING),
            (STATES["in_review"], [(ops.Q_ISSUE_STATE, None)]),
        )

    def test_move_without_frm_writes_only_when_elsewhere(self):
        for now, writes in (("todo", True), ("in_review", True), ("in_progress", False)):
            with self.subTest(now=now):
                left, calls = self.move(now, IssueState.RUNNING)
                self.assertIsNone(left)
                write = [(ops.M_STATE, STATES["in_progress"])] if writes else []
                self.assertEqual(calls, [(ops.Q_ISSUE_STATE, None), *write])

    def test_every_state_maps_to_its_id(self):
        for state, key in (
            (IssueState.READY, "todo"),
            (IssueState.RUNNING, "in_progress"),
            (IssueState.REVIEW, "in_review"),
            (IssueState.APPROVED, "handoff"),
            (IssueState.DONE, "done"),
            (IssueState.CANCELED, "canceled"),
        ):
            with self.subTest(state=state):
                gql = Texts(
                    {
                        ops.Q_ISSUE_STATE: {"issue": {"state": {"id": BACKLOG}}},
                        ops.M_STATE: {"issueUpdate": {"success": True}},
                    }
                )
                LinearTracker(gql, states=STATES).move("id-TASK-1", state)
                self.assertEqual(gql.calls[-1], (ops.M_STATE, {"i": "id-TASK-1", "s": STATES[key]}))

    def test_without_states_raises(self):
        with self.assertRaisesRegex(TypeError, "^LinearTracker needs states$"):
            LinearTracker(Texts({})).move("id-TASK-1", IssueState.RUNNING)


class WritebackWrites(Case):
    def test_comment_ref_is_client_id(self):
        gql = Texts(
            {
                ops.M_COMMENT: {"commentCreate": {"success": True}},
                ops.M_COMMENT_ID: {"commentCreate": {"success": True}},
            }
        )
        LinearTracker(gql).comment("id-TASK-7", "text")
        LinearTracker(gql).comment("id-TASK-7", "text", REF)
        self.assertEqual(
            gql.calls,
            [
                (ops.M_COMMENT, {"i": "id-TASK-7", "b": "text"}),
                (ops.M_COMMENT_ID, {"c": CID, "i": "id-TASK-7", "b": "text"}),
            ],
        )

    def test_comment_success_false_raises(self):
        gql = Texts({ops.M_COMMENT: {"commentCreate": {"success": False}}})
        with self.assertRaisesRegex(LinearError, "^commentCreate: success: false$"):
            LinearTracker(gql).comment("id-TASK-7", "text")

    def test_notify_notes_failures(self):
        gql = Texts(
            {
                ops.M_SUBSCRIBE: [
                    {"issueSubscribe": {"success": True}},
                    Unavailable("issueSubscribe", status=503),
                    {"issueSubscribe": {"success": False}},
                ]
            }
        )
        notes = LinearTracker(gql).notify("id-TASK-7", ("a@x.com", "b@x.com", "c@x.com"))
        self.assertEqual(
            [v for _, v in gql.calls],
            [{"i": "id-TASK-7", "e": e} for e in ("a@x.com", "b@x.com", "c@x.com")],
        )
        self.assertEqual(
            notes,
            "\n\nCould not subscribe b@x.com: Unavailable: issueSubscribe: HTTP 503"
            "\n\nCould not subscribe c@x.com: LinearError: issueSubscribe: success: false",
        )
        self.assertEqual(LinearTracker(Texts({})).notify("id-TASK-7", ()), "")

    def test_links_lists_urls(self):
        nodes = [{"url": "https://a"}, {"url": "https://b"}]
        gql = Texts({linear_tracker.Q_ATTACHED: {"issue": {"attachments": {"nodes": nodes}}}})
        self.assertEqual(LinearTracker(gql).links("id-TASK-7"), {"https://a", "https://b"})
        self.assertEqual(gql.calls, [(linear_tracker.Q_ATTACHED, {"i": "id-TASK-7"})])

    def test_link_tolerates_already_linked(self):
        for text in linear_tracker.ATTACHED:
            with self.subTest(error=text):
                gql = Texts({linear_tracker.M_ATTACH: LinearError(f"linear api error: {text}")})
                self.assertIsNone(LinearTracker(gql).link("id-TASK-7", "https://pr", "PR"))
                self.assertEqual(
                    gql.calls,
                    [(linear_tracker.M_ATTACH, {"i": "id-TASK-7", "u": "https://pr", "t": "PR"})],
                )

    def test_link_reraises_other_errors(self):
        for reply in (
            LinearError("linear api error: forbidden"),
            {"attachmentLinkURL": {"success": False}},
        ):
            with self.subTest(reply=reply), self.assertRaises(LinearError):
                LinearTracker(Texts({linear_tracker.M_ATTACH: reply})).link("i", "u", "t")

    def test_retitle(self):
        gql = Texts({linear_tracker.M_TITLE: {"issueUpdate": {"success": True}}})
        self.assertIsNone(LinearTracker(gql).retitle("id-TASK-7", "PRD: Registry"))
        self.assertEqual(
            gql.calls, [(linear_tracker.M_TITLE, {"i": "id-TASK-7", "t": "PRD: Registry"})]
        )
        gql = Texts({linear_tracker.M_TITLE: {"issueUpdate": {"success": False}}})
        with self.assertRaisesRegex(LinearError, "^issueUpdate: success: false$"):
            LinearTracker(gql).retitle("id-TASK-7", "PRD: Registry")

    def test_on_board_matches_team(self):
        for found, key in (
            ({"id": "id-TASK-3", "team": {"id": TEAM}}, "id-TASK-3"),
            ({"id": "id-OPS-3", "team": {"id": "other-team"}}, None),
            (None, None),
        ):
            with self.subTest(found=found):
                gql = Texts({linear_tracker.Q_ID: {"issue": found}})
                self.assertEqual(LinearTracker(gql, team_id=TEAM).on_board("TASK-3"), key)
                self.assertEqual(gql.calls, [(linear_tracker.Q_ID, {"i": "TASK-3"})])

    def test_unarchive(self):
        gql = Texts({linear_tracker.M_UNARCHIVE: {"issueUnarchive": {"success": True}}})
        self.assertIsNone(LinearTracker(gql).unarchive("id-TASK-3"))
        self.assertEqual(gql.calls, [(linear_tracker.M_UNARCHIVE, {"i": "id-TASK-3"})])


def users(*ids):
    return [{"users": {"nodes": [{"id": i}] if i else []}} for i in ids]


def board_cfg():
    return SimpleNamespace(
        team=TEAM,
        states=STATES,
        task_label_group=TASK_GROUP,
        task_labels={"light-research": "label-1"},
        agent_label_group="agents",
        agent_labels={"b": "label-b"},
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
        self.assertEqual((found.team_id, found.state_ids), (TEAM, STATES))
        self.assertEqual((found.roles, found.people), ({"u-pm": "pm"}, frozenset({"u-human"})))
        self.assertEqual(found.humans, ("human@x.test",))
        self.assertEqual(
            (found.task_group, found.label_tasks, found.agent_group, found.label_clients),
            (TASK_GROUP, {"label-1": "light-research"}, "agents", {"label-b": "b"}),
        )

    def test_errors_propagate(self):
        for replies, message in (
            ({"Q_TEAM": {"teams": {"nodes": []}}}, f"linear.team {TEAM} not found"),
            ({"Q_USER": users(None)}, "linear.human_members not found in Linear: human@x.test"),
            (
                {"Q_USER": users("u-human", None)},
                r"\[linear.roles.pm\]: account 'pm@agents.test' not found",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(LinearError, message):
                connected(**replies)


class TeamAndRoles(Case):
    def test_reads_the_team_then_the_role_accounts(self):
        gql = Gql(Q_TEAM={"teams": {"nodes": [team_node()]}}, Q_USER=users("u-pm"))
        found, roles = linear_tracker.team_and_roles(gql, board_cfg())
        self.assertEqual(gql.names(), ["Q_TEAM", "Q_USER"])
        self.assertEqual((found.id, found.states, roles), (TEAM, STATES, {"u-pm": "pm"}))

    def test_of_team_builds_from_the_team(self):
        found = LinearTracker.of_team(Gql(), TEAM_OBJ, ROLES, humans=HUMANS)
        self.assertEqual(
            (found.team_id, found.state_ids, found.finished_ids, found.roles, found.humans),
            (TEAM_OBJ.id, TEAM_OBJ.states, TEAM_OBJ.finished, ROLES, HUMANS),
        )
        self.assertEqual(LinearTracker.of_team(Gql(), TEAM_OBJ, ROLES).humans, ())


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


def todo(ident, *inverse):
    found = {"id": f"id-{ident}", "identifier": ident}
    return found | ({"inverseRelations": {"nodes": list(inverse)}} if inverse else {})


def rel(kind, ident=None, state="started"):
    return {"type": kind, "issue": ident and {"identifier": ident, "state": {"type": state}}}


def relations(*inverse):
    return {"issue": {"inverseRelations": {"nodes": list(inverse)}}}


class Ready(Case):
    def test_one_read_with_relations(self):
        nodes = [todo("TASK-1", rel("related", "TASK-9")), todo("TASK-2", rel("blocks", "TASK-7"))]
        gql = Gql(LIST_RELATIONS={"issues": {"nodes": nodes}})
        found = tracker(gql).ready()
        query = linear_tracker.q_issues(linear_tracker.RELATIONS)
        self.assertEqual(gql.calls, [(query, {"f": listed("todo")})])
        self.assertEqual(found, [(nodes[0], ()), (nodes[1], ("TASK-7",))])

    def fallback(self, *replies):
        said, error = [], LinearError("linear api error: boom")
        plain = {"issues": {"nodes": [todo("TASK-1"), todo("TASK-2")]}}
        gql = Gql(LIST_RELATIONS=error, LIST=plain, Q_RELATIONS=list(replies))
        found = LinearTracker(
            gql,
            states=TEAM_OBJ.states,
            team_id=TEAM,
            roles=ROLES,
            say=lambda *a, **f: said.append((len(gql.calls), a, f)),
        ).ready()
        self.assertEqual(said, [(1, ("todo-error",), {"error": one_line(error)})])
        self.assertEqual(gql.names(), ["LIST_RELATIONS", "LIST", "Q_RELATIONS", "Q_RELATIONS"])
        self.assertEqual([v for _, v in gql.calls[2:]], [{"i": "id-TASK-1"}, {"i": "id-TASK-2"}])
        return found

    def test_fallback_logs_then_reads_each(self):
        found = self.fallback(relations(rel("blocks", "TASK-7")), relations())
        self.assertEqual(
            found,
            [
                (todo("TASK-1", rel("blocks", "TASK-7")), ("TASK-7",)),
                ({**todo("TASK-2"), "inverseRelations": {"nodes": []}}, ()),
            ],
        )

    def test_unreadable_relations_block(self):
        found = self.fallback(LinearError("linear api error: gone"), relations())
        self.assertEqual(found[0], (todo("TASK-1", rel("blocks")), (UNREADABLE,)))
        self.assertEqual(found[1][1], ())

    def test_unavailable_propagates(self):
        error = Unavailable("issues", status=503)
        for replies in (
            {"LIST_RELATIONS": error},
            {
                "LIST_RELATIONS": LinearError("linear api error: boom"),
                "LIST": {"issues": {"nodes": [todo("TASK-1")]}},
                "Q_RELATIONS": error,
            },
        ):
            with self.subTest(fallback="LIST" in replies), self.assertRaises(Unavailable) as raised:
                tracker(Gql(**replies)).ready()
            self.assertIs(raised.exception, error)

    def test_blockers_open_and_unreadable_only(self):
        inverse = [
            rel("blocks", "TASK-3", "completed"),
            rel("blocks", "TASK-4", "canceled"),
            rel("blocks", "TASK-5", "duplicate"),
            rel("blocks", "TASK-6", "started"),
            rel("related", "TASK-8", "started"),
            rel("duplicate", None),
            rel("blocks"),
        ]
        gql = Gql(LIST_RELATIONS={"issues": {"nodes": [todo("TASK-1", *inverse)]}})
        self.assertEqual(tracker(gql).ready()[0][1], ("TASK-6", UNREADABLE))


def history(*moves):
    """Q_HISTORY's reply: (hours ago, state key, actor) moves, newest first."""
    nodes = [
        {"createdAt": ago(h).isoformat(), "actorId": actor, "toStateId": STATES[key]}
        for h, key, actor in moves
    ]
    return {"issue": {"history": {"nodes": nodes}}}


class MovedAt(Case):
    def tracker(self, *replies):
        gql = Gql(Q_HISTORY=list(replies))
        return LinearTracker(gql, states=TEAM_OBJ.states, people=frozenset({"u-human"}))

    def test_cached_per_key(self):
        found = self.tracker(history((2, "todo", "u-pm")), history((5, "todo", "u-pm")))
        self.assertEqual(found.moved_at("id-TASK-1", IssueState.READY), ago(2))
        self.assertIsNone(found.moved_at("id-TASK-1", IssueState.RUNNING))
        self.assertIsNone(found.moved_at("id-TASK-1", IssueState.READY, by_people=True))
        self.assertEqual(found.moved_at("id-TASK-2", IssueState.READY), ago(5))
        self.assertEqual(
            found.gql.calls,
            [
                (linear_tracker.Q_HISTORY, {"i": "id-TASK-1"}),
                (linear_tracker.Q_HISTORY, {"i": "id-TASK-2"}),
            ],
        )

    def test_by_people_filters_actors(self):
        found = self.tracker(history((1, "todo", "u-pm"), (3, "todo", "u-human")))
        self.assertEqual(found.moved_at("id-TASK-1", IssueState.READY), ago(1))
        self.assertEqual(found.moved_at("id-TASK-1", IssueState.READY, by_people=True), ago(3))

    def test_state_ids(self):
        moves = (1, "in_review", "u-pm"), (2, "in_progress", "u-pm"), (4, "todo", "u-human")
        found = self.tracker(history(*moves))
        self.assertEqual(found.moved_at("id-TASK-1", IssueState.RUNNING), ago(2))
        self.assertEqual(found.moved_at("id-TASK-1", IssueState.READY), ago(4))


def rechecked(state, *, assignee=None, project=None, labels=()):
    issue = {"state": {"id": state}, "assignee": assignee, "project": project}
    return {"issue": issue | {"labels": {"nodes": list(labels)}}}


PM, PROJECT = {"id": "u-pm", "email": "pm@agents.test"}, {"id": "p-1", "name": "Deep Research"}
AGENTS = "00000000-0000-4000-8000-000000000081"
CONFIGURED = "~/.agent-pm/config.local.toml's [orchestrator.linear"


def labelled(ident, name, group=TASK_GROUP):
    return {"id": ident, "name": name, "parent": group and {"id": group}}


class Rechecks(Case):
    def picker(self, *nodes):
        gql = Gql(Q_RECHECK=rechecked(STATES["todo"], assignee=PM, project=PROJECT, labels=nodes))
        found = LinearTracker(
            gql,
            states=TEAM_OBJ.states,
            roles=ROLES,
            task_group=TASK_GROUP,
            label_tasks={"l-light": "light-research", "l-deep": "deep-research"},
            agent_group=AGENTS,
            label_clients={"l-a": "a", "l-b": "b"},
        )
        return found.recheck("id-TASK-1")

    def test_maps_state_role_and_parts(self):
        # no label maps: no picks
        nodes = [labelled("l-1", "Light Research"), labelled("l-2", "Urgent", None)]
        gql = Gql(Q_RECHECK=rechecked(STATES["todo"], assignee=PM, project=PROJECT, labels=nodes))
        self.assertEqual(
            tracker(gql).recheck("id-TASK-1"),
            Recheck(
                ready=True,
                role="pm",
                refused=None,
                assignee=PM,
                home=PROJECT,
                task=Pick(),
                client=Pick(),
            ),
        )
        self.assertEqual(gql.calls, [(linear_tracker.Q_RECHECK, {"i": "id-TASK-1"})])

    def test_ready_only_in_the_todo_state(self):
        for key in ("in_progress", "in_review", "handoff", "done", "canceled"):
            with self.subTest(state=key):
                found = tracker(Gql(Q_RECHECK=rechecked(STATES[key]))).recheck("id-TASK-1")
                self.assertEqual(
                    found,
                    Recheck(
                        ready=False,
                        role=None,
                        refused="no project",
                        assignee=None,
                        home=None,
                        task=Pick(),
                        client=Pick(),
                    ),
                )

    def test_a_todo_id_shared_with_another_key_stays_ready(self):
        team = replace(TEAM_OBJ, states={**TEAM_OBJ.states, "in_progress": STATES["todo"]})
        gql = Gql(Q_RECHECK=rechecked(STATES["todo"]))
        self.assertTrue(tracker(gql, team).recheck("id-TASK-1").ready)

    def test_a_non_role_assignee_has_no_role(self):
        other = {"id": "u-other", "email": "other@x.test"}
        gql = Gql(Q_RECHECK=rechecked(STATES["todo"], assignee=other, project=PROJECT))
        found = tracker(gql).recheck("id-TASK-1")
        self.assertEqual((found.role, found.refused), (None, None))

    def test_picks(self):
        light, deep = labelled("l-light", "Light Research"), labelled("l-deep", "Deep Research")
        agent_a, agent_b = labelled("l-a", "Agent A", AGENTS), labelled("l-b", "Agent B", AGENTS)
        outside = (
            labelled("l-a", "Grouped elsewhere", "g-other"),
            labelled("l-b", "Ungrouped", None),
        )
        keep = "keep one, then move the issue back to Todo."
        fix = "Fix the label, then move the issue back to Todo."
        for nodes, task, client in (
            ((), Pick(), Pick()),
            (outside, Pick(), Pick()),
            ((light, agent_b, *outside), Pick("light-research"), Pick("b")),
            (
                (light, deep),
                Pick(error=f"Several task labels (Light Research, Deep Research); {keep}"),
                Pick(),
            ),
            (
                (agent_a, agent_b),
                Pick(),
                Pick(error=f"Several Agent labels (Agent A, Agent B); {keep}"),
            ),
            (
                (labelled("l-x", "Mystery"), labelled("l-y", "Robot", AGENTS)),
                Pick(error=f'Task label "Mystery" is not in {CONFIGURED}.task_labels]. {fix}'),
                Pick(error=f'Agent label "Robot" is not in {CONFIGURED}.agent_labels]. {fix}'),
            ),
        ):
            with self.subTest(nodes=nodes):
                found = self.picker(*nodes)
                self.assertEqual((found.task, found.client), (task, client))

    def test_no_agent_group_picks_no_client(self):
        found = LinearTracker(
            Gql(Q_RECHECK=rechecked(STATES["todo"], labels=[labelled("l-b", "Agent B", "")])),
            states=TEAM_OBJ.states,
            roles=ROLES,
            agent_group="",
            label_clients={"l-b": "b"},
        ).recheck("id-TASK-1")
        self.assertEqual(found.client, Pick())


class Role(Case):
    def test_by_assignee_id(self):
        self.assertEqual(tracker(Gql()).role({"assignee": {"id": "u-pm"}}), "pm")


class Interface(Case):
    def test_linear_tracker_implements_every_method(self):
        self.assertEqual(
            Tracker.__abstractmethods__,
            {
                "read",
                "finished",
                "sub_issues",
                "archive",
                "approved",
                "detail",
                "created",
                "create",
                "relate",
                "comment",
                "move",
                "claim",
                "comment_and_move",
                "find_comment",
                "put_comment",
                "notify",
                "links",
                "link",
                "retitle",
                "on_board",
                "unarchive",
                "role",
                "assigned",
                "ready",
                "moved_at",
                "recheck",
                "issue",
                "search",
                "description",
            },
        )
        self.assertIsInstance(LinearTracker(None), Tracker)


CREATED = "2026-09-27T10:00:00.000Z"
ONCE = ("id-DR-1", "pm", "2026-09-27T12:00:00.000Z")


def node(**user):
    return user or None


class ApprovedList(Case):
    def test_variables_and_records(self):
        found = {
            "id": "id-DR-1",
            "identifier": "DR-1",
            "url": "https://l/DR-1",
            "title": "Title",
            "priority": 2,
            "createdAt": CREATED,
            "project": {"id": "p-dr", "name": "Deep Research"},
            "assignee": {"id": "u-pm"},
            "attachments": {
                "nodes": [
                    {"title": "Report", "url": "https://x/report"},
                    {"title": "Run", "url": f"{linear_tracker.URL}abc"},
                ]
            },
        }
        gql = Gql(Q_HANDOFF={"issues": {"nodes": [found]}})
        listed = tracker(gql).approved(("engineer", "pm", "researcher"))
        self.assertEqual(
            gql.calls,
            [
                (
                    linear_tracker.Q_HANDOFF,
                    {
                        "t": TEAM,
                        "s": STATES["handoff"],
                        "a": ["u-researcher", "u-pm", "u-engineer"],
                    },
                )
            ],
        )
        self.assertEqual(
            listed,
            [
                Approved(
                    "id-DR-1",
                    "DR-1",
                    "https://l/DR-1",
                    "Title",
                    2,
                    CREATED,
                    "p-dr",
                    "Deep Research",
                    "pm",
                    (("Report", "https://x/report"),),
                )
            ],
        )


class DetailRead(Case):
    def test_states_moves_comments_related(self):
        review, handoff = STATES["in_review"], STATES["handoff"]
        run = "Run 00000000-0000-4000-8000-000000000099 · started"
        found = {
            "state": {"id": handoff},
            "history": {
                "nodes": [
                    {
                        "createdAt": "t4",
                        "actorId": "a",
                        "fromStateId": review,
                        "toStateId": handoff,
                    },
                    {"createdAt": "t3", "actorId": "a", "fromStateId": None, "toStateId": review},
                    {
                        "createdAt": "t2",
                        "actorId": "a",
                        "fromStateId": review,
                        "toStateId": DUPLICATE,
                    },
                    {
                        "createdAt": "t1",
                        "actorId": "a",
                        "fromStateId": BACKLOG,
                        "toStateId": STATES["done"],
                    },
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
            "comments": {
                "nodes": [
                    {
                        "body": "build X",
                        "createdAt": "c1",
                        "user": node(email="me@x.COM", name="Me"),
                    },
                    {"body": "hi", "createdAt": "c2", "user": node(email="bot@x.com", name="Bot")},
                    {"body": "synced", "createdAt": "c3", "user": None},
                    {
                        "body": run,
                        "createdAt": "c4",
                        "user": node(email="h@x.com", name="H", isMe=True),
                    },
                    {"body": "no email", "createdAt": "c5", "user": node(email=None, name="N")},
                ]
            },
            "relations": {"nodes": [{"relatedIssue": {"id": "id-A"}}]},
            "inverseRelations": {"nodes": [{"issue": {"id": "id-B"}}, {"issue": {"id": "id-A"}}]},
        }
        gql = Gql(Q_DETAIL={"issue": found})
        detail = tracker(gql).detail("id-DR-1")
        self.assertEqual(gql.calls, [(linear_tracker.Q_DETAIL, {"i": "id-DR-1"})])
        self.assertEqual(
            detail,
            Detail(
                IssueState.APPROVED,
                (
                    Move("t4", IssueState.APPROVED, IssueState.REVIEW),
                    Move("t3", IssueState.REVIEW, None),
                    Move("t2", IssueState.CANCELED, IssueState.REVIEW),
                    Move("t1", IssueState.DONE, None),
                ),
                (
                    Comment("c1", "build X", "Me", other=True, run=False),
                    Comment("c2", "hi", "Bot", other=False, run=False),
                    Comment("c3", "synced", "integration", other=False, run=False),
                    Comment("c4", run, "H", other=False, run=True),
                    Comment("c5", "no email", "N", other=False, run=False),
                ),
                frozenset({"id-A", "id-B"}),
            ),
        )


class NextIssue(Case):
    def test_created_by_once(self):
        key = ops.client_id(*ONCE)
        for nodes, want in (([{"id": key, "identifier": "C-1"}], Ref(key, "C-1")), ([], None)):
            with self.subTest(found=want):
                gql = Gql(Q_CHILD={"issues": {"nodes": nodes}})
                self.assertEqual(tracker(gql).created(ONCE), want)
                self.assertEqual(gql.calls, [(linear_tracker.Q_CHILD, {"c": key})])

    def test_create_variables(self):
        key = ops.client_id(*ONCE)
        reply = {"issueCreate": {"success": True, "issue": {"id": key, "identifier": "C-1"}}}
        gql = Gql(M_CREATE=reply)
        made = tracker(gql).create(
            ONCE, home="p-dr", owner="engineer", priority=3, title="ENG: T", description="D"
        )
        self.assertEqual(made, Ref(key, "C-1"))
        sent = {
            "id": key,
            "teamId": TEAM,
            "projectId": "p-dr",
            "assigneeId": "u-engineer",
            "stateId": STATES["todo"],
            "priority": 3,
            "title": "ENG: T",
            "description": "D",
        }
        self.assertEqual(gql.calls, [(linear_tracker.M_CREATE, {"in": sent})])
        self.assertEqual(list(gql.calls[0][1]["in"]), list(sent))

    def test_create_success_false_raises(self):
        gql = Gql(M_CREATE={"issueCreate": {"success": False}})
        with self.assertRaisesRegex(LinearError, "^issueCreate: success: false$"):
            tracker(gql).create(ONCE, home="p", owner="pm", priority=0, title="T", description="")


class Writes(Case):
    def test_relate(self):
        gql = Gql(M_RELATE={"issueRelationCreate": {"success": True}})
        tracker(gql).relate("id-A", "id-B")
        self.assertEqual(
            gql.calls,
            [
                (
                    linear_tracker.M_RELATE,
                    {"in": {"type": "related", "issueId": "id-A", "relatedIssueId": "id-B"}},
                )
            ],
        )

    def test_comment(self):
        gql = Gql(M_COMMENT={"commentCreate": {"success": True}})
        tracker(gql).comment("id-A", "Promoted to C-1.")
        self.assertEqual(gql.calls, [(ops.M_COMMENT, {"i": "id-A", "b": "Promoted to C-1."})])

    def test_move_maps_states_and_returns_current(self):
        for now, left, sent in (
            (STATES["handoff"], None, [(ops.M_STATE, {"i": "id-A", "s": STATES["done"]})]),
            (STATES["in_review"], STATES["in_review"], []),
        ):
            with self.subTest(now=now):
                gql = Gql(
                    Q_ISSUE_STATE={"issue": {"state": {"id": now}}},
                    M_STATE={"issueUpdate": {"success": True}},
                )
                moved = tracker(gql).move("id-A", IssueState.DONE, frm=IssueState.APPROVED)
                self.assertEqual(moved, left)
                self.assertEqual(gql.calls, [(ops.Q_ISSUE_STATE, {"i": "id-A"}), *sent])

    def test_claim_updates_without_reading(self):
        gql = Gql(M_STATE={"issueUpdate": {"success": True}})
        tracker(gql).claim("id-A")
        self.assertEqual(gql.calls, [(ops.M_STATE, {"i": "id-A", "s": STATES["in_progress"]})])

    def test_claim_without_states_raises(self):
        with self.assertRaisesRegex(TypeError, "^LinearTracker needs states$"):
            LinearTracker(Gql()).claim("id-A")

    def test_comment_and_move_subscribes_then_moves_then_comments(self):
        for notify, subscribed in ((True, list(HUMANS)), (False, [])):
            with self.subTest(notify=notify):
                gql = Gql(
                    M_SUBSCRIBE={"issueSubscribe": {"success": True}},
                    Q_ISSUE_STATE={"issue": {"state": {"id": STATES["handoff"]}}},
                    M_STATE={"issueUpdate": {"success": True}},
                    M_COMMENT={"commentCreate": {"success": True}},
                )
                left = tracker(gql).comment_and_move(
                    "id-A", "Back.", to=IssueState.REVIEW, frm=IssueState.APPROVED, notify=notify
                )
                self.assertIsNone(left)
                self.assertEqual(
                    gql.calls,
                    [
                        *((ops.M_SUBSCRIBE, {"i": "id-A", "e": e}) for e in subscribed),
                        (ops.Q_ISSUE_STATE, {"i": "id-A"}),
                        (ops.M_STATE, {"i": "id-A", "s": STATES["in_review"]}),
                        (ops.M_COMMENT, {"i": "id-A", "b": "Back."}),
                    ],
                )


def run_issue(**over):
    node = {
        "id": "uuid-7",
        "identifier": "TASK-7",
        "url": "https://linear.app/t/issue/TASK-7",
        "title": "Build it",
        "description": "Handoff from TASK-3: https://linear.app/t/issue/TASK-3\n\n## Instructions\nGo.",
        "createdAt": "2026-09-01T00:00:00.000Z",
        "project": {"id": "p-1"},
        "comments": {
            "nodes": [
                {"body": "later", "createdAt": "2026-09-03T00:00:00.000Z", "user": None},
                {
                    "body": f"Run {SID} · started",
                    "createdAt": "2026-09-02T00:00:00.000Z",
                    "user": {"email": "h@x.com", "name": "H", "isMe": True},
                },
                {
                    "body": "first",
                    "createdAt": "2026-09-01T12:00:00.000Z",
                    "user": {"email": "ann@x.com", "name": "Ann", "isMe": False},
                },
            ]
        },
        "attachments": {"nodes": [{"url": "https://github.com/o/r/pull/1"}]},
        "labels": {"nodes": [{"id": "l-1"}]},
        "relations": {
            "nodes": [
                {
                    "relatedIssue": {
                        "identifier": "TASK-2",
                        "title": "Two",
                        "state": {"name": "Done"},
                    }
                }
            ]
        },
        "inverseRelations": {
            "nodes": [
                {"issue": {"identifier": "TASK-2", "title": "Two", "state": {"name": "Done"}}},
                {"issue": {"identifier": "TASK-4", "title": "Four", "state": {"name": "Todo"}}},
            ]
        },
    }
    return {"issue": node | over}


class RunIssue(Case):
    def test_one_read_by_identifier_parsed(self):
        gql = Gql(Q_READ=run_issue())
        got = LinearTracker(gql).issue("TASK-7")
        self.assertEqual(gql.calls, [(linear_tracker.Q_READ, {"i": "TASK-7"})])
        self.assertEqual(
            got,
            Issue(
                "uuid-7",
                "TASK-7",
                "https://linear.app/t/issue/TASK-7",
                "Build it",
                run_issue()["issue"]["description"],
                "2026-09-01T00:00:00.000Z",
                "p-1",
                (
                    Note("first", "2026-09-01T12:00:00.000Z", "ann@x.com", "Ann"),
                    Note("later", "2026-09-03T00:00:00.000Z", None, None),
                ),
                (Link("https://github.com/o/r/pull/1"),),
                (Linked("TASK-2", "Two", "Done"), Linked("TASK-4", "Four", "Todo")),
                handoff=Handoff("TASK-3", "Go.", ""),
                labels=("l-1",),
            ),
        )

    def test_null_description_and_project(self):
        got = LinearTracker(Gql(Q_READ=run_issue(description=None, project=None))).issue("TASK-7")
        self.assertEqual((got.description, got.home, got.handoff), ("", None, None))

    def test_no_such_issue_or_another_raises(self):
        cases = (
            ({"issue": None}, "TASK-7: issue not found"),
            (run_issue(identifier="TASK-8"), "Linear returned 'TASK-8' for TASK-7"),
        )
        for reply, message in cases:
            with self.subTest(message=message), self.assertRaises(IssueError) as caught:
                LinearTracker(Gql(Q_READ=reply)).issue("TASK-7")
            self.assertEqual(str(caught.exception), message)


def candidate(ident, state, kind, *, urls=(), comment=None):
    return {
        "identifier": ident,
        "title": f"{ident} title",
        "description": None,
        "state": {"id": state, "name": f"{ident} state", "type": kind},
        "attachments": {"nodes": [{"url": u} for u in urls]},
        "comments": {"nodes": [] if comment is None else [{"body": comment}]},
    }


TEAM_F = {"team": {"id": {"eq": TEAM}}}


class Search(Case):
    def search(self, words, idents, **replies):
        gql = Gql(**replies)
        return gql, LinearTracker(gql, states=STATES, team_id=TEAM).search(words, idents)

    def test_the_query_follows_words_and_idents(self):
        found = {"searchIssues": {"nodes": [candidate("TASK-3", STATES["done"], "completed")]}}
        named = {"issues": {"nodes": [candidate("TASK-30", STATES["done"], "completed")]}}
        both = found | named
        numbers = {**TEAM_F, "number": {"in": [30, 4]}}
        cases = (
            (
                ("a", "b"),
                ("TASK-30", "TASK-4"),
                {"Q_SEARCH_IDS": both},
                "Q_SEARCH_IDS",
                {"t": "a b", "f": TEAM_F, "n": numbers},
                ["TASK-3", "TASK-30"],
            ),
            (("a",), (), {"Q_SEARCH": found}, "Q_SEARCH", {"t": "a", "f": TEAM_F}, ["TASK-3"]),
            ((), ("TASK-30", "TASK-4"), {"Q_IDS": named}, "Q_IDS", {"n": numbers}, ["TASK-30"]),
        )
        for words, idents, replies, name, variables, idents_found in cases:
            with self.subTest(query=name):
                gql, got = self.search(words, idents, **replies)
                self.assertEqual(gql.calls, [(getattr(linear_tracker, name), variables)])
                self.assertEqual([f.identifier for f in got], idents_found)

    def test_group_by_configured_state_then_type(self):
        nodes = [
            candidate("TASK-1", STATES["in_review"], "started"),
            candidate("TASK-2", "other", "started"),
            candidate("TASK-3", "other", "mystery"),
            candidate("TASK-4", STATES["canceled"], "canceled", comment="Not needed."),
            candidate("TASK-5", STATES["done"], "completed", comment="Shipped.", urls=["u\n"]),
        ]
        _, got = self.search(("a",), (), Q_SEARCH={"searchIssues": {"nodes": nodes}})
        self.assertEqual(
            got,
            (
                Found("TASK-1", "TASK-1 title", "TASK-1 state", Group.IN_REVIEW, (), "", ""),
                Found("TASK-2", "TASK-2 title", "TASK-2 state", Group.IN_PROGRESS, (), "", ""),
                Found("TASK-3", "TASK-3 title", "TASK-3 state", Group.BACKLOG, (), "", ""),
                Found(
                    "TASK-4", "TASK-4 title", "TASK-4 state", Group.CANCELED, (), "", "Not needed."
                ),
                Found("TASK-5", "TASK-5 title", "TASK-5 state", Group.DONE, ("u\n",), "", ""),
            ),
        )

    def test_no_words_no_idents_no_call(self):
        gql, got = self.search((), ())
        self.assertEqual((got, gql.calls), ((), []))


class Description(Case):
    def describe(self, reply):
        gql = Gql(Q_UPSTREAM=reply)
        got = LinearTracker(gql).description("TASK-5")
        self.assertEqual(gql.calls, [(linear_tracker.Q_UPSTREAM, {"i": "TASK-5"})])
        return got

    def test_its_description(self):
        issue = {"identifier": "TASK-5", "description": "Research."}
        self.assertEqual(self.describe({"issue": issue}), "Research.")
        self.assertEqual(self.describe({"issue": issue | {"description": None}}), "")

    def test_none_for_no_such_issue(self):
        missing = LinearError("linear api error: [{'message': 'Entity not found: Issue'}]")
        for reply in ({"issue": None}, missing):
            with self.subTest(reply=reply):
                self.assertIsNone(self.describe(reply))

    def test_another_error_raises(self):
        with self.assertRaises(LinearError):
            self.describe(LinearError("linear api error: boom"))


class RuleSeam(Case):
    def test_rule_modules_build_no_linear_query(self):
        for module in RULES:
            with self.subTest(module=module.__name__):
                tree = ast.parse(inspect.getsource(module))
                found = [
                    ast.unparse(n)
                    for n in ast.walk(tree)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and ("query(" in n.value or "mutation(" in n.value)
                    or isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id in ("gql", "call")
                    or isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id == "ops"
                    or isinstance(n, ast.ImportFrom)
                    and (n.module or "").endswith(".ops")
                    or isinstance(n, ast.alias)
                    and n.name == "ops"
                ]
                self.assertEqual(found, [])
