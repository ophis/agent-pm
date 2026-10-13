import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.engine import promote
from agent_pm.linear_orchestrator.engine.prune import types as prune
from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.linear import issues, linear, sessions, writeback
from orchestrator_tests.case import Case
from orchestrator_tests.integration import fake_linear
from orchestrator_tests.integration.fake_linear.fixtures import ServerCase
from orchestrator_tests.integration.fake_linear.seeds import PR, PROJECT
from orchestrator_tests.unit.board_ids import STATES, TEAM


class FixtureOperations(Case):
    def test_every_query_constant_has_an_op(self):
        want = {
            f"{name}.{key}": value
            for name, module in (
                ("linear", linear),
                ("router", routing),
                ("issues", issues),
                ("sessions", sessions),
                ("writeback", writeback),
                ("promote", promote),
                ("prune", prune),
            )
            for key, value in vars(module).items()
            if key[:2] in ("Q_", "M_") and isinstance(value, str)
        }
        want.update(
            {
                "router.issues": routing.q_issues(),
                "router.issues+relations": routing.q_issues(routing.RELATIONS),
            }
        )
        self.assertEqual(fake_linear.texts(), want)

    def test_two_names_for_one_text_are_refused(self):
        with self.assertRaisesRegex(ValueError, "a.X, b.Y"):
            fake_linear.index({"a.X": "q", "b.Y": "q"})

    def test_fail_refuses_a_misuse(self):
        fake = fake_linear.FakeLinear()
        fake.user("engineer@agents.test", "Engineer", "linear-api-key-engineer")
        fake.user("me@x.com", "Me")
        for args, kw in (
            (("linear.M_NOPE", "error"), {}),
            (("linear.M_STATE", "500"), {}),
            (("linear.Q_ISSUE_STATE", "unsuccessful"), {}),
            (("linear.M_STATE", "error"), {"times": 0}),
            (("linear.M_STATE", "error"), {"account": "nobody@agents.test"}),
            (("linear.M_STATE", "error"), {"account": "me@x.com"}),
        ):
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
        self.assertEqual(
            (fake_linear.NAMES[fake_linear.BACKLOG], fake_linear.TYPES["backlog"]),
            ("backlog", "backlog"),
        )

    def test_a_backlog_issue_moves_to_todo_and_back(self):
        fake = fake_linear.FakeLinear()
        actor = fake.user("me@x.com", "Me")
        self.assertEqual(
            fake.issue("Later", identifier="TASK-1", state="backlog")["state"], fake_linear.BACKLOG
        )
        fake.move("TASK-1", "todo", actor=actor)
        self.assertEqual(fake.find("TASK-1")["state"], STATES["todo"])
        fake.move("TASK-1", "backlog", actor=actor)
        task = fake.find("TASK-1")
        self.assertEqual(task["state"], fake_linear.BACKLOG)
        self.assertEqual(
            [
                (fake_linear.NAMES[h["fromStateId"]], fake_linear.NAMES[h["toStateId"]])
                for h in task["history"]
            ],
            [("backlog", "todo"), ("todo", "backlog")],
        )

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
        for t in (
            datetime(2026, 10, 9, 7, 30, 41, 62000),
            "2026-10-09T07:30:41.062",
            "2026-10-09T07:30:41.062Z",
            "2026-10-09T16:30:41.062+09:00",
            datetime(2026, 10, 9, 16, 30, 41, 62999, timezone(timedelta(hours=9))),
        ):
            with self.subTest(t):
                self.assertEqual(fake_linear.stamp(t), "2026-10-09T07:30:41.062Z")


class GraphQLQueries(ServerCase):
    def test_the_team_lists_backlog_beside_the_config_states(self):
        query = linear.Q_TEAM.replace(
            "states(first: 100) { nodes { id } }", "states(first: 100) { nodes { id name type } }"
        )
        self.fake.ops[query] = "linear.Q_TEAM"
        nodes = self.gql(query, t=TEAM)["teams"]["nodes"][0]["states"]["nodes"]
        self.assertEqual(
            {n["id"]: (n["name"], n["type"]) for n in nodes},
            {
                STATES["todo"]: ("Todo", "unstarted"),
                STATES["in_progress"]: ("In Progress", "started"),
                STATES["in_review"]: ("In Review", "started"),
                STATES["handoff"]: ("Handoff", "started"),
                STATES["done"]: ("Done", "completed"),
                STATES["canceled"]: ("Canceled", "canceled"),
                fake_linear.BACKLOG: ("Backlog", "backlog"),
            },
        )
        self.assertEqual(len(nodes), 7)
        cfg = SimpleNamespace(team=TEAM, states=STATES)
        self.assertEqual(linear.team(self.gql, cfg).states, STATES)

    def test_a_backlog_issue_is_not_in_a_todo_list(self):
        later = self.fake.issue("Later", state="backlog", assignee=self.engineer, project=PROJECT)
        ready = self.fake.issue("Ready", assignee=self.engineer, project=PROJECT)

        def listed(state):
            flt = {
                "team": {"id": {"eq": TEAM}},
                "project": {"null": False},
                "assignee": {"id": {"in": [self.engineer]}},
                "state": {"id": {"eq": state}},
            }
            return [n["identifier"] for n in self.gql(routing.q_issues(), f=flt)["issues"]["nodes"]]

        backlog = fake_linear.BACKLOG
        self.assertEqual(listed(STATES["todo"]), [ready["identifier"]])
        self.assertEqual(listed(backlog), [later["identifier"]])
        self.assertIsNone(linear.move(self.gql, later["id"], STATES["todo"], backlog))
        self.assertEqual(listed(STATES["todo"]), [later["identifier"], ready["identifier"]])
        self.assertIsNone(linear.move(self.gql, ready["id"], backlog, STATES["todo"]))
        self.assertEqual(
            (listed(STATES["todo"]), listed(backlog)),
            ([later["identifier"]], [ready["identifier"]]),
        )
        self.assertEqual(
            self.gql(linear.Q_ISSUE_STATE, i=ready["id"]), {"issue": {"state": {"id": backlog}}}
        )

    def test_issue_create_takes_the_backlog_state(self):
        new = {
            "id": str(uuid.uuid4()),
            "teamId": TEAM,
            "stateId": fake_linear.BACKLOG,
            "title": "Later",
        }
        linear.call(self.gql, promote.M_CREATE, "issueCreate", **{"in": new})
        self.assertEqual(self.fake.find(new["id"])["state"], fake_linear.BACKLOG)

    def test_relate_shows_in_relations_and_inverse_relations(self):
        self.fake.issue("Later", identifier="TASK-8", state="backlog")
        ready = self.fake.issue("Ready", identifier="TASK-9")
        self.fake.relate("blocks", "TASK-8", ready["id"])
        self.assertEqual(
            self.gql(issues.Q_ISSUE, i="TASK-8")["issue"]["relations"]["nodes"],
            [
                {
                    "relatedIssue": {
                        "identifier": "TASK-9",
                        "title": "Ready",
                        "state": {"name": "Todo"},
                    }
                }
            ],
        )
        self.assertEqual(
            self.gql(issues.Q_ISSUE, i="TASK-9")["issue"]["inverseRelations"]["nodes"],
            [{"issue": {"identifier": "TASK-8", "title": "Later", "state": {"name": "Backlog"}}}],
        )

    def test_the_write_back_ops(self):
        prd = self.fake.issue("PRD: Widgets", state="done")
        linear.call(
            self.gql,
            promote.M_RELATE,
            "issueRelationCreate",
            **{"in": {"type": "related", "issueId": prd["id"], "relatedIssueId": "TASK-7"}},
        )
        for _ in range(2):
            linear.call(self.eng, writeback.M_ATTACH, "attachmentLinkURL", i="TASK-7", u=PR, t="PR")
        self.assertEqual(
            self.eng(writeback.Q_ATTACHED, i="TASK-7"),
            {"issue": {"attachments": {"nodes": [{"url": PR}]}}},
        )
        self.assertEqual(linear.subscribe(self.eng, "TASK-7", ["ME@x.com"]), "")
        linear.call(
            self.eng, writeback.M_TITLE, "issueUpdate", i="TASK-7", t="ENG: Session registry"
        )
        task = self.fake.find("TASK-7")
        with self.fake.lock:
            task["archived"] = True
        self.assertEqual(
            self.gql(writeback.Q_ID, i="TASK-7"),
            {"issue": {"id": task["id"], "team": {"id": TEAM}}},
        )
        read = issues.read_issue(self.gql, "TASK-7")
        self.assertEqual(
            (read.title, read.project_id, read.linked),
            (
                "ENG: Session registry",
                PROJECT["id"],
                (issues.Linked(prd["identifier"], "PRD: Widgets", "Done"),),
            ),
        )
        linear.call(self.eng, writeback.M_UNARCHIVE, "issueUnarchive", i=task["id"])
        self.assertEqual(
            (task["attachments"], task["subscribers"], task["archived"]),
            ([{"url": PR, "title": "PR"}], ["Me@X.com"], False),
        )

    def test_the_promote_and_prune_reads(self):
        created, handed, said, done = (f"2026-10-0{d}T07:00:00.000Z" for d in (5, 6, 7, 8))
        src = self.fake.issue(
            "Research",
            identifier="TASK-9",
            state="in_review",
            assignee=self.engineer,
            project=PROJECT,
            priority=2,
            created=created,
        )
        self.fake.move("TASK-9", "handoff", actor=self.human, at=handed)
        self.fake.comment("TASK-9", self.human, "Build it.", at=said)
        linear.call(self.eng, writeback.M_ATTACH, "attachmentLinkURL", i="TASK-9", u=PR, t="PR")
        linear.call(
            self.gql,
            promote.M_RELATE,
            "issueRelationCreate",
            **{"in": {"type": "related", "issueId": "TASK-7", "relatedIssueId": "TASK-9"}},
        )
        self.assertEqual(
            self.gql(promote.Q_HANDOFF, t=TEAM, s=STATES["handoff"], a=[self.engineer])["issues"][
                "nodes"
            ],
            [
                {
                    "id": src["id"],
                    "identifier": "TASK-9",
                    "url": src["url"],
                    "title": "Research",
                    "priority": 2,
                    "createdAt": created,
                    "project": PROJECT,
                    "assignee": {"id": self.engineer},
                    "attachments": {"nodes": [{"title": "PR", "url": PR}]},
                }
            ],
        )
        self.assertEqual(
            self.gql(promote.Q_DETAIL, i=src["id"])["issue"],
            {
                "state": {"id": STATES["handoff"]},
                "history": {
                    "nodes": [
                        {
                            "createdAt": handed,
                            "actorId": self.human,
                            "fromStateId": STATES["in_review"],
                            "toStateId": STATES["handoff"],
                        }
                    ]
                },
                "comments": {
                    "nodes": [
                        {
                            "body": "Build it.",
                            "createdAt": said,
                            "user": {"email": "Me@X.com", "name": "Me", "isMe": False},
                        }
                    ]
                },
                "relations": {"nodes": []},
                "inverseRelations": {"nodes": [{"issue": {"id": self.fake.find("TASK-7")["id"]}}]},
            },
        )
        self.fake.move("TASK-9", "done", actor=self.harness, at=done)
        finished = self.gql(prune.Q_ISSUE, i="TASK-9")["issue"]
        self.assertEqual(finished["state"], {"id": STATES["done"]})
        self.assertEqual(
            linear.last_move(finished["history"]["nodes"], {STATES["done"]}),
            linear.parse_time(done),
        )
