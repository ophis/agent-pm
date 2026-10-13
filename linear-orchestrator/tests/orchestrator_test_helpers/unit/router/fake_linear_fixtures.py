from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import TEAM, team_node
from orchestrator_test_helpers.unit.router.data import (
    DEEP,
    LIGHT,
    NAMES,
    ORPHAN_LABEL,
    PD,
    RECHECK,
    ROLE,
    STATES,
    USER,
    issue,
    op,
)


class FakeLinear:
    def __init__(self, issues, history=None):
        self.issues = {i["identifier"]: i for i in issues}
        self.history = {} if history is None else history
        self.mutations, self.queries, self.services = [], [], []
        self.todo_error = False
        self.unreadable = set()
        self.refuse = set()
        self.group = {
            "isGroup": True,
            "children": {"nodes": [{"id": i} for i in (LIGHT, DEEP, ORPHAN_LABEL)]},
        }

    def __call__(self, query, *, service, **v):
        self.queries.append((query, v))
        self.services.append(service)
        if query == linear.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == linear.Q_TASK_GROUP:
            return {"issueLabel": self.group}
        if "users(filter" in query:
            users = {
                "me@x.com": USER,
                "b@x.com": "user-b",
                **{a.lower(): f"u-{r}" for r, a in ROLE.items()},
            }
            return {
                "users": {"nodes": [{"id": u} for e, u in users.items() if v["e"].lower() == e]}
            }
        if "history" in query:
            return {"issue": {"history": {"nodes": self.history.get(v["i"], [])}}}
        if "mutation" in query:
            self.mutations.append((query, v))
            issue = self.issues[v["i"]]
            if "issueSubscribe" in query:
                issue.setdefault("subscribers", []).append(v["e"])
            elif "commentCreate" in query:
                issue.setdefault("comments", []).append(v["b"])
            elif v["i"] in self.refuse:
                return {"issueUpdate": {"success": False}}
            else:
                issue["state"] = NAMES[v["s"]]
            return {op(query): {"success": True}}
        if query == linear.Q_ISSUE_STATE:
            return {"issue": {"state": {"id": STATES[self.issues[v["i"]]["state"]]}}}
        if "issues(filter" in query:
            f = v["f"]
            if self.todo_error and "inverseRelations" in query:
                raise LinearError("linear api error: [{'message': 'Entity not found'}]")
            drop = (
                {"state", "labels"}
                if "inverseRelations" in query
                else {"state", "labels", "inverseRelations"}
            )
            return {
                "issues": {
                    "nodes": [
                        {k: x for k, x in i.items() if k not in drop}
                        for i in self.issues.values()
                        if f["team"]["id"]["eq"] == TEAM
                        and f["project"] == {"null": False}
                        and i["project"]
                        and i["assignee"]
                        and i["assignee"]["id"] in f["assignee"]["id"]["in"]
                        and i["state"] == NAMES[f["state"]["id"]["eq"]]
                    ]
                }
            }
        if "issue(id:" in query and "inverseRelations" in query:
            if v["i"] in self.unreadable:
                raise LinearError("linear api error: [{'message': 'Entity not found'}]")
            return {"issue": {"inverseRelations": self.issues[v["i"]]["inverseRelations"]}}
        if query == RECHECK:
            i = self.issues[v["i"]]
            return {
                "issue": {
                    "state": {"id": STATES[i["state"]]},
                    "assignee": i["assignee"],
                    "project": i["project"],
                    "labels": {"nodes": i["labels"]},
                }
            }
        raise AssertionError(query)

    def reads(self, state="Todo"):
        return [
            q
            for q, v in self.queries
            if "issues(filter" in q and v["f"].get("state") == {"id": {"eq": STATES[state]}}
        ]


def two():
    return FakeLinear(
        [
            issue("TASK-1", "Todo", "researcher", priority=1),
            issue("TASK-2", "Todo", "pm", priority=2, project=PD),
        ]
    )
