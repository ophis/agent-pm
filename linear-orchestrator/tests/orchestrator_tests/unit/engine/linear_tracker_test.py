"""The Linear adapter of the flow Tracker: prune's and write-back's query text and its reads and
writes."""

import ast
import hashlib
import inspect
from dataclasses import replace
from datetime import timedelta

from agent_pm.linear_orchestrator.engine import linear_tracker, ops, prune, run_comments, writeback
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
    "M_TITLE": "1b74f5b2bb7775a1147c8a6bac7f50c09a196de9122da2c52e7cd64eaf45d7be",
    "M_ATTACH": "8db2cfd3ed91f6b4daf1aeed80183275d14953ff02982847658243be789af1f4",
    "M_UNARCHIVE": "e0c0d3bff0bf11847fc7ffd4b98030ac98c9319cde37a09fe0745906d545d9a3",
    "Q_ATTACHED": "63dbe2b07f16ec03a33efb47cf14ddb32a3bac690ae5a62ac57c43c57fbe3c96",
    "Q_ID": "339eb6885dd45c62cfae3c2f1d2bbdcdf6995a2e008079832d405f3019f5adf9",
    "M_UPDATE": "95eaa4a0cccf407f93c0137e32a54edfd12fcc68a35f4c94b2ad407ad59377dd",
}
REEXPORTS = {
    prune: ("Q_ISSUE", "Q_FINISHED", "Q_CHILDREN", "M_ARCHIVE"),
    writeback: ("M_TITLE", "M_ATTACH", "M_UNARCHIVE", "Q_ATTACHED", "Q_ID"),
    run_comments: ("M_UPDATE",),
}
REF = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d/session"
CID = ops.client_id("0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d", "session")
NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")
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


def tracker(gql, team=TEAM_OBJ):
    return LinearTracker(
        gql, states=team.states, team_id=team.id, finished=team.finished, roles=ROLES
    )


class Queries(Case):
    def test_text_is_the_base_text(self):
        for name, digest in BASE.items():
            with self.subTest(query=name):
                text = getattr(linear_tracker, name)
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)

    def test_reexports_are_the_adapter_strings(self):
        for module, names in REEXPORTS.items():
            for name in names:
                with self.subTest(module=module.__name__, query=name):
                    self.assertIs(getattr(module, name), getattr(linear_tracker, name))


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
        args = (to,) if frm is None else (to, frm)
        left = LinearTracker(gql, states=STATES).move("id-TASK-1", *args)
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

    def test_guarded_move_already_in_to_returns_none(self):
        self.assertEqual(
            self.move("in_review", IssueState.REVIEW, IssueState.RUNNING),
            (None, [(ops.Q_ISSUE_STATE, None)]),
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


class Writes(Case):
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


class Interface(Case):
    def test_linear_tracker_implements_every_method(self):
        self.assertEqual(
            Tracker.__abstractmethods__,
            {
                "read",
                "finished",
                "sub_issues",
                "archive",
                "find_comment",
                "put_comment",
                "move",
                "comment",
                "notify",
                "links",
                "link",
                "retitle",
                "on_board",
                "unarchive",
            },
        )
        self.assertIsInstance(LinearTracker(None), Tracker)


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


class WritebackSeam(Case):
    def test_writeback_and_run_comments_reach_linear_only_through_the_tracker(self):
        for module in (writeback, run_comments):
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
