from orchestrator_tests.unit.writeback_data import FIELDS, NAMES, NOT_FOUND, STATES, mine


class Gql:
    def __init__(
        self,
        states=(STATES["in_progress"],),
        attachments=(),
        fail=lambda _name, _v: None,
        issue=None,
        comments=None,
    ):
        self.calls, self.states, self.attachments, self.fail, self.issue = (
            [],
            list(states),
            attachments,
            fail,
            issue,
        )
        self.comments = dict(comments or {})

    def __call__(self, query, **v):
        name = NAMES[query]
        self.calls.append((name, v))
        r = self.fail(name, v)
        if isinstance(r, BaseException):
            raise r
        if name == "read":
            return {"issue": {"attachments": {"nodes": [{"url": u} for u in self.attachments]}}}
        if name == "reread":
            s = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"issue": {"state": {"id": s}}}
        if name == "id":
            return {"issue": self.issue}
        if name == "find":
            c = self.comments.get(v["c"])
            if c is None:
                raise NOT_FOUND
            user = {"isMe": c["mine"]}
            return {
                "comment": {
                    "id": v["c"],
                    "body": c["body"],
                    "issue": {"id": c["issue"]},
                    "user": user,
                }
            }
        if name == "comment" and "c" in v and r is not False:
            self.comments[v["c"]] = mine(v["b"], v["i"])
        return {FIELDS[name]: {"success": r is not False}}
