import re
import uuid

from orchestrator_tests.integration.fake_linear.data import (
    COMPARATORS,
    CREATE,
    IDS,
    KEY,
    NAMES,
    TITLES,
    TYPES,
    newest,
    stamp,
)
from orchestrator_tests.integration.fixture_errors import Problem
from orchestrator_tests.unit.board_ids import STATES, TEAM


class Operations:
    def _get(self, ref):
        if (rec := self.find(ref)) is None:
            raise Problem.missing_entity("Issue")
        return rec

    def _add(self, id, identifier, **fields):
        numbers = [int(i["identifier"].split("-")[1]) for i in self.issues.values()]
        identifier = identifier or f"{KEY}-{max(numbers, default=0) + 1}"
        if self.find(id) or self.find(identifier) or not re.fullmatch(rf"{KEY}-\d+", identifier):
            raise Problem.invalid_issue(id, identifier, KEY)
        self.issues[id] = {
            "id": id,
            "identifier": identifier,
            "url": f"https://linear.app/fake/issue/{identifier}",
            **fields,
            "comments": [],
            "attachments": [],
            "subscribers": [],
            "history": [],
            "archived": False,
        }
        return self.issues[id]

    def _comment(self, rec, user, body, at, cid=None):
        if cid is None:
            cid = str(uuid.uuid4())
        elif self._comment_of(cid) is not None:
            raise Problem.duplicate_comment(cid)
        rec["comments"].append({"id": cid, "body": body, "createdAt": at, "user": user})
        return cid

    def _move(self, rec, state, actor, at):
        if state != rec["state"]:
            rec["history"].append(
                {"createdAt": at, "actorId": actor, "fromStateId": rec["state"], "toStateId": state}
            )
            rec["state"], rec["updatedAt"] = state, at

    def _state(self, sid):
        return {"id": sid, "name": TITLES[NAMES[sid]], "type": TYPES[NAMES[sid]]}

    def _team(self):
        return {
            "id": TEAM,
            "name": "Team",
            "key": KEY,
            "states": {"nodes": [self._state(i) for i in IDS.values()]},
        }

    def _person(self, uid, caller):
        u = self.users.get(uid) if uid else None
        return u and {
            "id": u["id"],
            "email": u["email"],
            "name": u["name"],
            "isMe": u["id"] == caller["id"],
        }

    def _label(self, lid):
        parent = self.labels[lid]["parent"]
        return {
            **{k: self.labels[lid][k] for k in ("id", "name", "isGroup")},
            "parent": parent and {"id": parent},
        }

    def _brief(self, iid):
        rec = self.issues[iid]
        return {
            **{k: rec[k] for k in ("id", "identifier", "title", "url")},
            "state": self._state(rec["state"]),
        }

    def _view(self, rec, caller):
        out = {
            k: rec[k]
            for k in (
                "id",
                "identifier",
                "url",
                "title",
                "description",
                "priority",
                "createdAt",
                "updatedAt",
                "project",
            )
        }
        return {
            **out,
            "state": self._state(rec["state"]),
            "team": self._team(),
            "assignee": self._person(rec["assignee"], caller),
            "labels": {"nodes": [self._label(i) for i in rec["labels"]]},
            "comments": {
                "nodes": newest(
                    [{**c, "user": self._person(c["user"], caller)} for c in rec["comments"]]
                )
            },
            "attachments": {"nodes": rec["attachments"]},
            "history": {"nodes": newest(rec["history"])},
            "relations": {
                "nodes": [
                    {"type": t, "issue": self._brief(a), "relatedIssue": self._brief(b)}
                    for t, a, b in self.relations
                    if a == rec["id"]
                ]
            },
            "inverseRelations": {
                "nodes": [
                    {"type": t, "issue": self._brief(a), "relatedIssue": self._brief(b)}
                    for t, a, b in self.relations
                    if b == rec["id"]
                ]
            },
        }

    def _list(self, caller, flt, first, after=None, *, archived=False):
        found = [
            i
            for i in self.issues.values()
            if (archived or not i["archived"]) and self._match(i, flt)
        ]
        start = int(after or 0)
        page = found[start : start + first]
        return {
            "issues": {
                "nodes": [self._view(i, caller) for i in page],
                "pageInfo": {
                    "hasNextPage": start + first < len(found),
                    "endCursor": str(start + len(page)) if page else None,
                },
            }
        }

    def _match(self, rec, flt):
        values = {
            "id": rec["id"],
            "team": TEAM,
            "state": rec["state"],
            "assignee": rec["assignee"],
            "project": rec["project"] and rec["project"]["id"],
        }
        for field, cond in flt.items():
            if field not in values:
                raise Problem.unknown_filter(field)
            for c, arg in (cond["id"] if field != "id" and "id" in cond else cond).items():
                if c not in COMPARATORS:
                    raise Problem.unknown_comparator(c)
                if not COMPARATORS[c](values[field], arg):
                    return False
        return True

    def handle_issue(self, caller, v):
        return {"issue": self._view(self._get(v["i"]), caller)}

    def _comment_of(self, cid):
        return next(
            ((i, c) for i in self.issues.values() for c in i["comments"] if c["id"] == cid), None
        )

    def handle_users(self, caller, v):
        return {
            "users": {
                "nodes": [
                    self._person(i, caller)
                    for i, u in self.users.items()
                    if u["email"].lower() == v["e"].lower()
                ]
            }
        }

    def handle_teams(self, _caller, v):
        return {"teams": {"nodes": [self._team()] if v.get("t") == TEAM else []}}

    def handle_label_group(self, _caller, v):
        if v["i"] not in self.labels:
            raise Problem.missing_entity("IssueLabel")
        children = [self._label(i) for i, label in self.labels.items() if label["parent"] == v["i"]]
        return {"issueLabel": {**self._label(v["i"]), "children": {"nodes": children}}}

    def handle_board(self, caller, v):
        return self._list(caller, v.get("f") or {}, 100)

    def handle_handoff(self, caller, v):
        return self._list(
            caller,
            {
                "team": {"id": {"eq": v.get("t")}},
                "state": {"id": {"eq": v.get("s")}},
                "project": {"null": False},
                "assignee": {"id": {"in": v.get("a")}},
            },
            100,
        )

    def handle_child(self, caller, v):
        return self._list(caller, {"id": {"eq": v["c"]}}, 50, archived=True)

    def handle_finished(self, caller, v):
        return self._list(
            caller,
            {
                "team": {"id": {"eq": v.get("t")}},
                "state": {"id": {"in": v.get("s")}},
                "assignee": {"id": {"in": v.get("a")}},
            },
            50,
            v.get("c"),
        )

    def handle_comment_create(self, caller, v, cid=None):
        self._comment(self._get(v["i"]), caller["id"], v["b"], stamp(), cid)
        return {"commentCreate": {"success": True}}

    def handle_comment_create_id(self, caller, v):
        return self.handle_comment_create(caller, v, v["c"])

    def handle_comment_by_id(self, caller, v):
        if (found := self._comment_of(v["c"])) is None:
            raise Problem.missing_entity("Comment")
        rec, c = found
        return {
            "comment": {**c, "issue": {"id": rec["id"]}, "user": self._person(c["user"], caller)}
        }

    def handle_comment_update(self, caller, v):
        if (found := self._comment_of(v["c"])) is None:
            raise Problem.missing_entity("Comment")
        _, c = found
        if c["user"] != caller["id"]:
            raise Problem.foreign_comment()
        c["body"] = v["b"]
        return {"commentUpdate": {"success": True}}

    def handle_set_state(self, caller, v):
        if v["s"] not in NAMES:
            raise Problem.missing_entity("WorkflowState")
        self._move(self._get(v["i"]), v["s"], caller["id"], stamp())
        return {"issueUpdate": {"success": True}}

    def handle_set_title(self, _caller, v):
        rec = self._get(v["i"])
        rec["title"], rec["updatedAt"] = v["t"], stamp()
        return {"issueUpdate": {"success": True}}

    def handle_subscribe(self, _caller, v):
        rec = self._get(v["i"])
        user = next((u for u in self.users.values() if u["email"].lower() == v["e"].lower()), None)
        if user is None:
            raise Problem.missing_entity("User")
        if user["email"] not in rec["subscribers"]:
            rec["subscribers"].append(user["email"])
        return {"issueSubscribe": {"success": True}}

    def handle_attach(self, _caller, v):
        rec = self._get(v["i"])
        if all(a["url"] != v["u"] for a in rec["attachments"]):
            rec["attachments"].append({"url": v["u"], "title": v.get("t")})
        return {"attachmentLinkURL": {"success": True}}

    def handle_archive(self, _caller, v):
        self._get(v["i"])["archived"] = True
        return {"issueArchive": {"success": True}}

    def handle_unarchive(self, _caller, v):
        self._get(v["i"])["archived"] = False
        return {"issueUnarchive": {"success": True}}

    def handle_create(self, caller, v):
        new, now = v["in"], stamp()
        pid, uid, sid = (
            new.get("projectId"),
            new.get("assigneeId"),
            new.get("stateId", STATES["todo"]),
        )
        if extra := sorted(set(new) - CREATE):
            raise Problem.unknown_create_input(extra[0])
        if (
            new.get("teamId") != TEAM
            or (pid and pid not in self.projects)
            or (uid and uid not in self.users)
            or sid not in NAMES
        ):
            raise Problem.missing_entities(("Team", "Project", "User", "WorkflowState"))
        rec = self._add(
            new.get("id") or str(uuid.uuid4()),
            None,
            title=new.get("title", ""),
            description=new.get("description"),
            priority=new.get("priority", 0),
            state=sid,
            assignee=uid,
            project=pid and {"id": pid, "name": self.projects[pid]},
            labels=[],
            createdAt=now,
            updatedAt=now,
        )
        return {"issueCreate": {"success": True, "issue": self._view(rec, caller)}}

    def handle_relate(self, _caller, v):
        new = v["in"]
        self.relations.append(
            (new["type"], self._get(new["issueId"])["id"], self._get(new["relatedIssueId"])["id"])
        )
        return {"issueRelationCreate": {"success": True}}
