from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import issues, linear
from orchestrator_tests.unit.board_ids import TEAM, team_node
from orchestrator_tests.unit.router.data import (
    DEEP,
    LIGHT,
    NAMES,
    ORPHAN_LABEL,
    PD,
    RECHECK,
    ROLE,
    STATES,
    USER,
    IssueFacts,
    ago,
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
        if (value := self._identity(query, v)) is not None:
            return value
        if "history" in query:
            return self._issue_read(query, v)
        if "mutation" in query:
            return self._mutate(query, v)
        if "issues(filter" in query:
            return self._queue(query, v)
        if (value := self._issue_read(query, v)) is not None:
            return value
        raise AssertionError(query)

    def _identity(self, query, v):
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
        return None

    def _issue_read(self, query, v):
        if "history" in query:
            return {"issue": {"history": {"nodes": self.history.get(v["i"], [])}}}
        if query == linear.Q_ISSUE_STATE:
            return {"issue": {"state": {"id": STATES[self.issues[v["i"]]["state"]]}}}
        if "issue(id:" in query and "inverseRelations" in query:
            if v["i"] in self.unreadable:
                raise LinearError.api([{"message": "Entity not found"}])
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
        return None

    def _mutate(self, query, v):
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

    def _queue(self, query, v):
        f = v["f"]
        if self.todo_error and "inverseRelations" in query:
            raise LinearError.api([{"message": "Entity not found"}])
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
                    and (i["assignee"]["id"] in f["assignee"]["id"]["in"])
                    and (i["state"] == NAMES[f["state"]["id"]["eq"]])
                ]
            }
        }

    def reads(self, state="Todo"):
        return [
            q
            for q, v in self.queries
            if "issues(filter" in q and v["f"].get("state") == {"id": {"eq": STATES[state]}}
        ]


class Moved(FakeLinear):
    def __init__(self, issues, to):
        super().__init__(issues)
        self.to = to

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if "issues(filter" in query:
            for i in self.issues.values():
                i["state"] = self.to
        return out


class Changed(FakeLinear):
    """Each list read is followed by `after`'s edits ({ident: fields}): a change before the claim's
    recheck.
    """

    def __init__(self, issues, after):
        super().__init__(issues)
        self.after = after

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if "issues(filter" in query:
            for ident, fields in self.after.items():
                self.issues[ident].update(fields)
        return out


class LostClaim(FakeLinear):
    def __init__(self, issues):
        super().__init__(issues)
        self.lose = True

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if query == linear.M_STATE and self.lose:
            self.lose = False
            self.issues[v["i"]]["updatedAt"] = ago(minutes=0)
            raise linear.Unavailable("issueUpdate", reason="TimeoutError: timed out")
        return out


class ClaimLinear(FakeLinear):
    """FakeLinear plus the outer's issue read, answered with `node`."""

    def __init__(self, todo, node):
        super().__init__([todo])
        self.node = node

    def __call__(self, query, **v):
        if query == issues.Q_ISSUE:
            self.queries.append((query, v))
            return {"issue": self.node}
        return super().__call__(query, **v)


def two():
    return FakeLinear(
        [
            issue("TASK-1", "Todo", "researcher", facts=IssueFacts(priority=1)),
            issue("TASK-2", "Todo", "pm", facts=IssueFacts(priority=2, project=PD)),
        ]
    )
