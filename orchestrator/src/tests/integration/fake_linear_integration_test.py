"""fake_linear.py served over HTTP, reached through linear.linear_gql with AGENT_PM_LINEAR set in this process's env."""
import functools
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [TESTS, os.path.dirname(TESTS)]
import hermetic  # noqa: E402,F401
from board_ids import STATES, TASK_GROUP, TEAM  # noqa: E402
import linear  # noqa: E402
import issues  # noqa: E402
import promote  # noqa: E402
import prune  # noqa: E402
import router  # noqa: E402
import sessions  # noqa: E402
import writeback  # noqa: E402
import fake_linear  # noqa: E402

HARNESS, ENGINEER = "linear-api-key", "linear-api-key-engineer"
HARNESS_EMAIL, ENGINEER_EMAIL = "harness@agents.test", "engineer@agents.test"
PROJECT = {"id": "121166b1-191a-4461-bec4-42f1c2dc0ddd", "name": "Widgets"}
LABEL = "00000000-0000-4000-8000-000000000031"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
PR = "https://github.com/acme/widgets/pull/1"
VIEWER = "query { viewer { id } }"
LINEAR_TIME = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"


class Table(unittest.TestCase):
    def test_every_query_constant_has_an_op(self):
        want = {f"{m.__name__}.{k}": v for m in (linear, router, issues, sessions, writeback, promote, prune)
                for k, v in vars(m).items() if k[:2] in ("Q_", "M_") and isinstance(v, str)}
        want.update({"router.issues": router.q_issues(), "router.issues+relations": router.q_issues(router.RELATIONS)})
        self.assertEqual(fake_linear.texts(), want)

    def test_two_names_for_one_text_are_refused(self):
        with self.assertRaisesRegex(ValueError, "a.X, b.Y"):
            fake_linear.index({"a.X": "q", "b.Y": "q"})

    def test_fail_refuses_a_misuse(self):
        fake = fake_linear.FakeLinear()
        fake.user("engineer@agents.test", "Engineer", "linear-api-key-engineer")
        fake.user("me@x.com", "Me")
        for args, kw in ((("linear.M_NOPE", "error"), {}), (("linear.M_STATE", "500"), {}),
                         (("linear.Q_ISSUE_STATE", "unsuccessful"), {}), (("linear.M_STATE", "error"), {"times": 0}),
                         (("linear.M_STATE", "error"), {"account": "nobody@agents.test"}),
                         (("linear.M_STATE", "error"), {"account": "me@x.com"})):
            with self.subTest(args, **kw), self.assertRaises(ValueError):
                fake.fail(*args, **kw)
        fake.fail("linear.M_STATE", "error", account="Engineer@agents.test")

    def test_seeding_refuses_a_used_or_malformed_issue_id(self):
        fake = fake_linear.FakeLinear()
        uid = fake.issue(identifier="TASK-7")["id"]
        for kw in ({"identifier": "TASK-7"}, {"id": uid}, {"identifier": "OTHER-1"}):
            with self.subTest(**kw), self.assertRaises(ValueError):
                fake.issue(**kw)

    def test_backlog_is_a_state_outside_the_config(self):
        self.assertNotIn(fake_linear.BACKLOG, STATES.values())
        self.assertEqual((fake_linear.NAMES[fake_linear.BACKLOG], fake_linear.TYPES["backlog"]), ("backlog", "backlog"))

    def test_a_backlog_issue_moves_to_todo_and_back(self):
        fake = fake_linear.FakeLinear()
        actor = fake.user("me@x.com", "Me")
        self.assertEqual(fake.issue("Later", identifier="TASK-1", state="backlog")["state"], fake_linear.BACKLOG)
        fake.move("TASK-1", "todo", actor=actor)
        self.assertEqual(fake.find("TASK-1")["state"], STATES["todo"])
        fake.move("TASK-1", "backlog", actor=actor)
        task = fake.find("TASK-1")
        self.assertEqual(task["state"], fake_linear.BACKLOG)
        self.assertEqual([(fake_linear.NAMES[h["fromStateId"]], fake_linear.NAMES[h["toStateId"]])
                          for h in task["history"]], [("backlog", "todo"), ("todo", "backlog")])

    def test_relate_records_the_issues_ids_and_refuses_an_unknown_one(self):
        fake = fake_linear.FakeLinear()
        a, b = fake.issue("A", identifier="TASK-1"), fake.issue("B", identifier="TASK-2")
        fake.relate("blocks", "TASK-1", b["id"])
        self.assertEqual(fake.relations, [("blocks", a["id"], b["id"])])
        for refs in (("TASK-1", "TASK-9"), ("TASK-9", "TASK-2"), (a["id"], "no-such-id")):
            with self.subTest(refs), self.assertRaises(ValueError):
                fake.relate("blocks", *refs)
        self.assertEqual(len(fake.relations), 1)

    def test_a_time_without_an_offset_is_utc(self):
        tz = mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"})
        self.addCleanup(time.tzset)
        tz.start()
        self.addCleanup(tz.stop)
        time.tzset()
        for t in (datetime(2026, 10, 9, 7, 30, 41, 62000), "2026-10-09T07:30:41.062", "2026-10-09T07:30:41.062Z",
                  "2026-10-09T16:30:41.062+09:00", datetime(2026, 10, 9, 16, 30, 41, 62999, timezone(timedelta(hours=9)))):
            with self.subTest(t):
                self.assertEqual(fake_linear.stamp(t), "2026-10-09T07:30:41.062Z")


class Served(unittest.TestCase):
    def setUp(self):
        self.fake = fake = fake_linear.FakeLinear(harness=HARNESS)
        self.harness = fake.user(HARNESS_EMAIL, "Harness", HARNESS)
        self.engineer = fake.user(ENGINEER_EMAIL, "Engineer", ENGINEER)
        self.human = fake.user("Me@X.com", "Me")
        fake.issue("Session registry", identifier="TASK-7", state="in_progress", assignee=self.engineer, project=PROJECT)
        self.url = fake_linear.serve(self, fake)
        env = mock.patch.dict(os.environ, {linear.SEAM: fake_linear.seam(self, fake, self.url)})
        env.start()
        self.addCleanup(env.stop)
        self.gql = functools.partial(linear.linear_gql, service=HARNESS)
        self.eng = functools.partial(linear.linear_gql, service=ENGINEER)

    def log(self):
        return [(r.op, r.account, r.result) for r in self.fake.requests]

    def test_projection_keeps_only_the_selection(self):
        want = {"issue": {"state": {"id": STATES["in_progress"]}}}
        self.assertEqual(self.gql(linear.Q_ISSUE_STATE, i="TASK-7"), want)
        self.assertEqual(self.gql(linear.Q_ISSUE_STATE, i=self.fake.find("TASK-7")["id"]), want)

    def test_a_selected_field_the_model_lacks_is_an_error(self):
        query = linear.Q_ISSUE_STATE.replace("state { id }", "state { id color }")
        self.fake.ops[query] = "linear.Q_ISSUE_STATE"
        with self.assertRaises(SystemExit) as cm:
            self.gql(query, i="TASK-7")
        self.assertIn("fake: linear.Q_ISSUE_STATE has no field color", cm.exception.code)
        self.assertEqual(self.log(), [("linear.Q_ISSUE_STATE", HARNESS_EMAIL, "error")])

    def test_is_me_follows_the_key(self):
        mine = {HARNESS: self.fake.comment("TASK-7", self.harness, f"Run {SID} · running"),
                ENGINEER: self.fake.comment("TASK-7", self.engineer, f"Run {SID} · not a session")}
        self.fake.comment("TASK-7", None, "From an integration")
        for service, gql in ((HARNESS, self.gql), (ENGINEER, self.eng)):
            with self.subTest(service):
                nodes = gql(issues.Q_ISSUE, i="TASK-7")["issue"]["comments"]["nodes"]
                self.assertEqual({n["body"]: (n["user"] or {}).get("isMe") for n in nodes},
                                 {f"Run {SID} · running": service == HARNESS, f"Run {SID} · not a session": service == ENGINEER,
                                  "From an integration": None})
                found = gql(sessions.Q_FIND, i="TASK-7", p=sessions.prefix(SID))["issue"]["comments"]["nodes"]
                self.assertEqual([c["id"] for c in found], [mine[service]])
        self.assertIsNone(sessions.write(self.gql, "TASK-7", SID, f"Run {SID} · done"))
        with self.assertRaises(SystemExit):
            self.eng(sessions.M_UPDATE, c=mine[HARNESS], b="hijacked")
        self.assertEqual(sorted(c["body"] for c in self.fake.find("TASK-7")["comments"]),
                         ["From an integration", f"Run {SID} · done", f"Run {SID} · not a session"])

    def test_a_move_is_history_by_the_caller(self):
        self.fake.issue("Claimed", identifier="TASK-8")
        self.fake.move("TASK-8", "in_progress", actor=self.harness, at=datetime(2026, 10, 8, 7, 30, 41, 62000, timezone.utc))
        self.assertIsNone(linear.move(self.eng, "TASK-8", STATES["in_review"], STATES["in_progress"]))
        nodes = self.gql(router.Q_HISTORY, i="TASK-8")["issue"]["history"]["nodes"]
        self.assertEqual([(n["actorId"], n["fromStateId"], n["toStateId"]) for n in nodes],
                         [(self.engineer, STATES["in_progress"], STATES["in_review"]),
                          (self.harness, STATES["todo"], STATES["in_progress"])])
        self.assertEqual(nodes[1]["createdAt"], "2026-10-08T07:30:41.062Z")
        self.assertRegex(nodes[0]["createdAt"], LINEAR_TIME)
        self.assertLess(datetime.now(timezone.utc) - linear.parse_time(nodes[0]["createdAt"]), timedelta(seconds=10))
        self.assertEqual(self.fake.find("TASK-8")["state"], STATES["in_review"])

    def test_each_failure_kind_as_the_caller_sees_it(self):
        for name, eng in (("http", self.eng), ("in process", functools.partial(self.fake, service=ENGINEER))):
            with self.subTest(name):
                self.fake.requests.clear()
                self.fake.fail("linear.M_COMMENT", "error")
                with self.assertRaises(SystemExit) as cm:
                    linear.comment(eng, "TASK-7", "lost")
                self.assertIsInstance(cm.exception.code, str)
                self.fake.fail("linear.M_STATE", "unsuccessful")
                with self.assertRaisesRegex(RuntimeError, "^issueUpdate: success: false$"):
                    linear.move(eng, "TASK-7", STATES["in_review"], STATES["in_progress"])
                self.fake.fail("linear.M_SUBSCRIBE", "503")
                with self.assertRaises(linear.Unavailable) as cm:
                    linear.call(eng, linear.M_SUBSCRIBE, "issueSubscribe", i="TASK-7", e="me@x.com")
                self.assertEqual(str(cm.exception), "issueSubscribe: HTTP 503")
                self.fake.fail("linear.Q_ISSUE_STATE", "hang", seconds=3)
                start = time.monotonic()
                with self.assertRaises(linear.Unavailable) as cm:
                    eng(linear.Q_ISSUE_STATE, timeout=1, i="TASK-7")
                self.assertEqual(str(cm.exception), "issue: TimeoutError: timed out")
                self.assertLess(time.monotonic() - start, 2.5)
                self.assertEqual(self.log(), [("linear.M_COMMENT", ENGINEER_EMAIL, "error"),
                                              ("linear.Q_ISSUE_STATE", ENGINEER_EMAIL, "ok"),
                                              ("linear.M_STATE", ENGINEER_EMAIL, "unsuccessful"),
                                              ("linear.M_SUBSCRIBE", ENGINEER_EMAIL, "503"),
                                              ("linear.Q_ISSUE_STATE", ENGINEER_EMAIL, "hang")])
        task = self.fake.find("TASK-7")
        self.assertEqual((task["state"], task["comments"], task["subscribers"]), (STATES["in_progress"], [], []))

    def test_account_and_times_scope_a_failure(self):
        self.fake.fail("linear.M_COMMENT", "error", account="Engineer@agents.test", times=2)
        linear.comment(self.gql, "TASK-7", "by the harness")
        for _ in range(2):
            with self.assertRaises(SystemExit):
                linear.comment(self.eng, "TASK-7", "lost")
        linear.comment(self.eng, "TASK-7", "by the engineer")
        self.assertEqual(self.log(), [("linear.M_COMMENT", HARNESS_EMAIL, "ok"), ("linear.M_COMMENT", ENGINEER_EMAIL, "error"),
                                      ("linear.M_COMMENT", ENGINEER_EMAIL, "error"), ("linear.M_COMMENT", ENGINEER_EMAIL, "ok")])
        self.assertEqual([c["body"] for c in self.fake.find("TASK-7")["comments"]], ["by the harness", "by the engineer"])

    def test_an_unknown_query_and_a_bad_key_are_errors_and_logged(self):
        with self.assertRaises(SystemExit) as cm:
            self.gql(VIEWER)
        self.assertIn("fake: unknown query", cm.exception.code)
        stranger = fake_linear.FakeLinear()
        stranger.user("stranger@x.com", "Stranger", "stranger")
        with mock.patch.dict(os.environ, {linear.SEAM: fake_linear.seam(self, stranger, self.url)}), \
                self.assertRaises(urllib.error.HTTPError) as cm:
            linear.linear_gql(linear.Q_USER, service="stranger", e="me@x.com")
        cm.exception.close()
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(self.fake.requests, [fake_linear.Request(VIEWER, HARNESS_EMAIL, {}, "unknown"),
                                              fake_linear.Request("linear.Q_USER", None, {"e": "me@x.com"}, "unauthorized")])

    def test_only_post_graphql_is_served(self):
        send = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        for req, code in ((self.url, 405), (urllib.request.Request(self.url.replace("/graphql", "/other"), b"{}"), 404)):
            with self.subTest(code), self.assertRaises(urllib.error.HTTPError) as cm:
                send(req, timeout=5)
            cm.exception.close()
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.fake.requests, [])

    def test_a_bad_call_is_a_logged_error(self):
        with self.assertRaises(SystemExit) as cm:
            self.gql(linear.Q_ISSUE_STATE)
        self.assertIn("fake: linear.Q_ISSUE_STATE: KeyError: 'i'", cm.exception.code)
        with self.assertRaises(SystemExit) as cm:
            self.gql(prune.Q_FINISHED, t=TEAM, s=None, a=[self.engineer], c=None)
        self.assertIn("fake: prune.Q_FINISHED: TypeError: ", cm.exception.code)
        self.assertEqual(self.log(), [("linear.Q_ISSUE_STATE", HARNESS_EMAIL, "error"),
                                      ("prune.Q_FINISHED", HARNESS_EMAIL, "error")])

    def test_a_bad_body_is_a_logged_400(self):
        send, key = urllib.request.build_opener(urllib.request.ProxyHandler({})).open, self.fake.keys()[HARNESS]
        for body in (b"not json", b"[]", b'{"variables": {}}', b'{"query": "q", "variables": []}'):
            with self.subTest(body), self.assertRaises(urllib.error.HTTPError) as cm:
                send(urllib.request.Request(self.url, body, {"Authorization": key}), timeout=5)
            cm.exception.close()
            self.assertEqual(cm.exception.code, 400)
        self.assertEqual(self.fake.requests, [fake_linear.Request(None, HARNESS_EMAIL, {}, "error")] * 4)

    def test_finished_issues_page(self):
        finished = [self.fake.issue(f"Finished {n}", state=("done", "canceled")[n % 2], assignee=self.engineer)["identifier"]
                    for n in range(51)]
        archived = self.fake.issue("Archived", state="done", assignee=self.engineer)["identifier"]
        linear.call(self.gql, prune.M_ARCHIVE, "issueArchive", i=archived)
        self.fake.issue("Someone else's", state="done", assignee=self.human)
        pages, cursor = [], None
        while True:
            page = self.gql(prune.Q_FINISHED, t=TEAM, s=[STATES["done"], STATES["canceled"]], a=[self.engineer],
                            c=cursor)["issues"]
            pages.append([n["identifier"] for n in page["nodes"]])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        self.assertEqual([len(p) for p in pages], [50, 1])
        self.assertEqual(sum(pages, []), finished)

    def test_issue_create_takes_the_next_number_and_refuses_a_used_id(self):
        self.fake.issue("Older", identifier="TASK-3")
        new = {"id": promote.child_id("src", "pm", "2026-10-09T07:00:00.000Z"), "teamId": TEAM, "projectId": PROJECT["id"],
               "assigneeId": self.engineer, "stateId": STATES["todo"], "priority": 2, "title": "PRD: Widgets",
               "description": "Handoff from TASK-7: https://linear.app/fake/issue/TASK-7"}
        made = linear.call(self.gql, promote.M_CREATE, "issueCreate", **{"in": new})
        self.assertEqual(made, {"success": True, "issue": {"id": new["id"], "identifier": "TASK-8"}})
        self.assertEqual(self.gql(promote.Q_CHILD, c=new["id"]), {"issues": {"nodes": [made["issue"]]}})
        child = self.fake.find("TASK-8")
        self.assertEqual({k: child[k] for k in ("id", "state", "assignee", "project", "priority", "title", "description")},
                         {"id": new["id"], "state": STATES["todo"], "assignee": self.engineer, "project": PROJECT,
                          "priority": 2, "title": "PRD: Widgets", "description": new["description"]})
        with self.assertRaises(SystemExit) as cm:
            linear.call(self.gql, promote.M_CREATE, "issueCreate", **{"in": dict(new, title="Again")})
        self.assertIn(new["id"], cm.exception.code)
        self.assertEqual([r.result for r in self.fake.requests], ["ok", "ok", "error"])
        self.assertIsNone(self.fake.find("TASK-9"))

    def test_the_board_reads(self):
        self.fake.label_group(TASK_GROUP, "Tasks", {LABEL: "build"})
        blocker = self.fake.issue("Blocker", state="in_progress")
        ready = self.fake.issue("Ready", assignee=self.engineer, project=PROJECT, labels=[LABEL])
        self.fake.issue("No project", assignee=self.engineer)
        self.fake.issue("Someone else's", assignee=self.human, project=PROJECT)
        linear.call(self.gql, promote.M_RELATE, "issueRelationCreate",
                    **{"in": {"type": "blocks", "issueId": blocker["id"], "relatedIssueId": ready["id"]}})
        self.assertEqual(linear.team(self.gql, {"team": TEAM, "states": STATES}).states, STATES)
        linear.task_group(self.gql, {"task_label_group": TASK_GROUP, "task_labels": {"build": LABEL}})
        self.assertEqual(linear.user_id(self.gql, "me@x.com"), self.human)
        flt = {"team": {"id": {"eq": TEAM}}, "project": {"null": False}, "assignee": {"id": {"in": [self.engineer]}},
               "state": {"id": {"eq": STATES["todo"]}}}
        nodes = self.gql(router.q_issues(router.RELATIONS), f=flt)["issues"]["nodes"]
        self.assertEqual([n["identifier"] for n in nodes], [ready["identifier"]])
        self.assertEqual(router.blockers(nodes[0]), [blocker["identifier"]])
        self.assertNotIn("inverseRelations", self.gql(router.q_issues(), f=flt)["issues"]["nodes"][0])
        self.assertEqual(self.gql(router.Q_RECHECK, i=ready["id"])["issue"],
                         {"state": {"id": STATES["todo"]}, "labels": {"nodes": [{"id": LABEL, "name": "build",
                                                                                 "parent": {"id": TASK_GROUP}}]}})
        self.fake.move(blocker["id"], "done", actor=self.harness)
        self.assertEqual(router.blockers(self.gql(router.Q_RELATIONS, i=ready["id"])["issue"]), [])

    def test_the_team_lists_backlog_beside_the_config_states(self):
        query = linear.Q_TEAM.replace("states(first: 100) { nodes { id } }", "states(first: 100) { nodes { id name type } }")
        self.fake.ops[query] = "linear.Q_TEAM"
        nodes = self.gql(query, t=TEAM)["teams"]["nodes"][0]["states"]["nodes"]
        self.assertEqual({n["id"]: (n["name"], n["type"]) for n in nodes}, {
            STATES["todo"]: ("Todo", "unstarted"), STATES["in_progress"]: ("In Progress", "started"),
            STATES["in_review"]: ("In Review", "started"), STATES["handoff"]: ("Handoff", "started"),
            STATES["done"]: ("Done", "completed"), STATES["canceled"]: ("Canceled", "canceled"),
            fake_linear.BACKLOG: ("Backlog", "backlog")})
        self.assertEqual(len(nodes), 7)
        self.assertEqual(linear.team(self.gql, {"team": TEAM, "states": STATES}).states, STATES)

    def test_a_backlog_issue_is_not_in_a_todo_list(self):
        later = self.fake.issue("Later", state="backlog", assignee=self.engineer, project=PROJECT)
        ready = self.fake.issue("Ready", assignee=self.engineer, project=PROJECT)

        def listed(state):
            flt = {"team": {"id": {"eq": TEAM}}, "project": {"null": False},
                   "assignee": {"id": {"in": [self.engineer]}}, "state": {"id": {"eq": state}}}
            return [n["identifier"] for n in self.gql(router.q_issues(), f=flt)["issues"]["nodes"]]

        backlog = fake_linear.BACKLOG
        self.assertEqual(listed(STATES["todo"]), [ready["identifier"]])
        self.assertEqual(listed(backlog), [later["identifier"]])
        self.assertIsNone(linear.move(self.gql, later["id"], STATES["todo"], backlog))
        self.assertEqual(listed(STATES["todo"]), [later["identifier"], ready["identifier"]])
        self.assertIsNone(linear.move(self.gql, ready["id"], backlog, STATES["todo"]))
        self.assertEqual((listed(STATES["todo"]), listed(backlog)), ([later["identifier"]], [ready["identifier"]]))
        self.assertEqual(self.gql(linear.Q_ISSUE_STATE, i=ready["id"]), {"issue": {"state": {"id": backlog}}})

    def test_issue_create_takes_the_backlog_state(self):
        new = {"id": str(uuid.uuid4()), "teamId": TEAM, "stateId": fake_linear.BACKLOG, "title": "Later"}
        linear.call(self.gql, promote.M_CREATE, "issueCreate", **{"in": new})
        self.assertEqual(self.fake.find(new["id"])["state"], fake_linear.BACKLOG)

    def test_relate_shows_in_relations_and_inverse_relations(self):
        later = self.fake.issue("Later", identifier="TASK-8", state="backlog")
        ready = self.fake.issue("Ready", identifier="TASK-9")
        self.fake.relate("blocks", "TASK-8", ready["id"])
        self.assertEqual(self.gql(issues.Q_ISSUE, i="TASK-8")["issue"]["relations"]["nodes"],
                         [{"relatedIssue": {"identifier": "TASK-9", "title": "Ready", "state": {"name": "Todo"}}}])
        self.assertEqual(self.gql(issues.Q_ISSUE, i="TASK-9")["issue"]["inverseRelations"]["nodes"],
                         [{"issue": {"identifier": "TASK-8", "title": "Later", "state": {"name": "Backlog"}}}])
        self.assertEqual(self.gql(router.Q_RELATIONS, i="TASK-9")["issue"]["inverseRelations"]["nodes"],
                         [{"type": "blocks", "issue": {"identifier": "TASK-8", "state": {"type": "backlog"}}}])
        self.assertEqual(router.blockers(self.gql(router.Q_RELATIONS, i=ready["id"])["issue"]), [later["identifier"]])

    def test_the_write_back_ops(self):
        prd = self.fake.issue("PRD: Widgets", state="done")
        linear.call(self.gql, promote.M_RELATE, "issueRelationCreate",
                    **{"in": {"type": "related", "issueId": prd["id"], "relatedIssueId": "TASK-7"}})
        for _ in range(2):
            linear.call(self.eng, writeback.M_ATTACH, "attachmentLinkURL", i="TASK-7", u=PR, t="PR")
        self.assertEqual(self.eng(writeback.Q_ATTACHED, i="TASK-7"), {"issue": {"attachments": {"nodes": [{"url": PR}]}}})
        self.assertEqual(linear.subscribe(self.eng, "TASK-7", ["ME@x.com"]), "")
        linear.call(self.eng, writeback.M_TITLE, "issueUpdate", i="TASK-7", t="ENG: Session registry")
        linear.call(self.gql, prune.M_ARCHIVE, "issueArchive", i="TASK-7")
        flt = {"team": {"id": {"eq": TEAM}}, "state": {"id": {"eq": STATES["in_progress"]}}}
        self.assertEqual(self.gql(router.q_issues(), f=flt), {"issues": {"nodes": []}})
        task = self.fake.find("TASK-7")
        self.assertEqual(self.gql(writeback.Q_ID, i="TASK-7"), {"issue": {"id": task["id"], "team": {"id": TEAM}}})
        read = issues.read_issue(self.gql, "TASK-7")
        self.assertEqual((read.title, read.project_id, read.linked),
                         ("ENG: Session registry", PROJECT["id"], (issues.Linked(prd["identifier"], "PRD: Widgets", "Done"),)))
        linear.call(self.eng, writeback.M_UNARCHIVE, "issueUnarchive", i=task["id"])
        self.assertEqual([n["identifier"] for n in self.gql(router.q_issues(), f=flt)["issues"]["nodes"]], ["TASK-7"])
        self.assertEqual((task["attachments"], task["subscribers"], task["archived"]),
                         ([{"url": PR, "title": "PR"}], ["Me@X.com"], False))

    def test_the_promote_and_prune_reads(self):
        created, handed, said, done = (f"2026-10-0{d}T07:00:00.000Z" for d in (5, 6, 7, 8))
        src = self.fake.issue("Research", identifier="TASK-9", state="in_review", assignee=self.engineer, project=PROJECT,
                              priority=2, created=created)
        self.fake.move("TASK-9", "handoff", actor=self.human, at=handed)
        self.fake.comment("TASK-9", self.human, "Build it.", at=said)
        linear.call(self.eng, writeback.M_ATTACH, "attachmentLinkURL", i="TASK-9", u=PR, t="PR")
        linear.call(self.gql, promote.M_RELATE, "issueRelationCreate",
                    **{"in": {"type": "related", "issueId": "TASK-7", "relatedIssueId": "TASK-9"}})
        self.assertEqual(self.gql(promote.Q_HANDOFF, t=TEAM, s=STATES["handoff"], a=[self.engineer])["issues"]["nodes"],
                         [{"id": src["id"], "identifier": "TASK-9", "url": src["url"], "title": "Research", "priority": 2,
                           "createdAt": created, "project": PROJECT, "assignee": {"id": self.engineer},
                           "attachments": {"nodes": [{"title": "PR", "url": PR}]}}])
        self.assertEqual(self.gql(promote.Q_DETAIL, i=src["id"])["issue"], {
            "state": {"id": STATES["handoff"]},
            "history": {"nodes": [{"createdAt": handed, "actorId": self.human, "fromStateId": STATES["in_review"],
                                   "toStateId": STATES["handoff"]}]},
            "comments": {"nodes": [{"body": "Build it.", "createdAt": said,
                                    "user": {"email": "Me@X.com", "name": "Me", "isMe": False}}]},
            "relations": {"nodes": []}, "inverseRelations": {"nodes": [{"issue": {"id": self.fake.find("TASK-7")["id"]}}]}})
        self.fake.move("TASK-9", "done", actor=self.harness, at=done)
        finished = self.gql(prune.Q_ISSUE, i="TASK-9")["issue"]
        self.assertEqual(finished["state"], {"id": STATES["done"]})
        self.assertEqual(linear.last_move(finished["history"]["nodes"], {STATES["done"]}), linear.parse_time(done))


if __name__ == "__main__":
    unittest.main()
