import uuid
from types import SimpleNamespace

from agent_pm.linear_orchestrator.engine import promote, prune, router
from agent_pm.linear_orchestrator.linear import issues, linear, writeback
from orchestrator_tests.integration import fake_linear
from orchestrator_tests.integration.fake_linear_integration_data import PR, PROJECT
from orchestrator_tests.integration.fake_linear_integration_scenarios import ServedScenario
from orchestrator_tests.unit.board_ids import STATES, TEAM


class ServedTheTeamListsBacklogBeside(ServedScenario):
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
            return [n["identifier"] for n in self.gql(router.q_issues(), f=flt)["issues"]["nodes"]]

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
