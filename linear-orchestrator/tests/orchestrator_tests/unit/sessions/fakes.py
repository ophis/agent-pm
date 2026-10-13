from agent_pm.linear_orchestrator.linear import linear, sessions
from orchestrator_tests.unit.sessions.data import CID, ISSUE, NOT_FOUND, comment


class FakeLinear:
    def __init__(self, *comments, fail=None):
        self.comments = {c["id"]: dict(c) for c in comments}
        self.fail, self.calls = dict(fail or {}), []

    def __call__(self, query, **v):
        op = next(o for o in ("commentUpdate", "commentCreate", "comment") if o + "(" in query)
        self.calls.append(op)
        if op in self.fail:
            if self.fail[op] is False:
                return {op: {"success": False}}
            raise self.fail[op]
        if op == "comment":
            assert query == linear.Q_COMMENT, query
            c = self.comments.get(v["c"])
            if c is None:
                raise NOT_FOUND
            user = {"isMe": c["mine"]}
            return {
                "comment": {
                    "id": c["id"],
                    "body": c["body"],
                    "issue": {"id": c["issue"]},
                    "user": user,
                }
            }
        if op == "commentCreate":
            assert query == linear.M_COMMENT_ID and v["i"] == ISSUE, (query, v)
            assert v["c"] not in self.comments, "fake: comment id in use"
            self.comments[v["c"]] = comment(v["c"], v["b"])
        else:
            assert query == sessions.M_UPDATE, query
            if v["c"] not in self.comments:
                raise NOT_FOUND
            self.comments[v["c"]]["body"] = v["b"]
        return {op: {"success": True}}

    def only(self):
        assert len(self.comments) == 1, self.comments
        assert CID in self.comments, self.comments
        return self.comments[CID]
