import functools
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_pm.linear_orchestrator.engine import config, promote, prune
from agent_pm.linear_orchestrator.engine import router as routing
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import issues, linear, sessions, writeback
from orchestrator_tests.integration.fixture_errors import FakeCredentialError, FixtureError, Problem
from orchestrator_tests.unit.board_ids import STATES, TEAM


@dataclass
class Request:
    op: str | None
    account: str | None
    variables: dict
    result: str


@dataclass
class InjectedFailure:
    op: str
    kind: str
    account: str | None
    times: int
    seconds: float


@dataclass(frozen=True, kw_only=True)
class IssueSeed:
    identifier: str | None = None
    id: str | None = None
    state: str = "todo"
    assignee: str | None = None
    project: dict | None = None
    description: str = ""
    priority: int = 0
    labels: tuple = ()
    created: str | None = None
    updated: str | None = None


@dataclass(frozen=True)
class HttpReply:
    status: int
    payload: dict
    delay: float = 0


KEY = "TASK"

BACKLOG = "00000000-0000-4000-8000-000000000017"

IDS = {**STATES, "backlog": BACKLOG}

TYPES = {
    "todo": "unstarted",
    "in_progress": "started",
    "in_review": "started",
    "handoff": "started",
    "done": "completed",
    "canceled": "canceled",
    "backlog": "backlog",
}

NAMES = {i: k for k, i in IDS.items()}

TITLES = {**config.STATES, "backlog": "Backlog"}

KINDS = ("error", "unsuccessful", "503", "hang")

MODULES = {
    "linear": linear,
    "issues": issues,
    "sessions": sessions,
    "writeback": writeback,
    "promote": promote,
    "prune": prune,
    "router": routing,
}

BUILT = {
    "router.issues": routing.q_issues(),
    "router.issues+relations": routing.q_issues(routing.RELATIONS),
}

CREATE = {"id", "teamId", "projectId", "assigneeId", "stateId", "priority", "title", "description"}

COMPARATORS = {
    "eq": lambda x, a: x == a,
    "in": lambda x, a: x in a,
    "null": lambda x, a: (x is None) == a,
}


def stamp(t=None):
    if t is None:
        t = datetime.now(UTC)
    elif isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    t = (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def credential_key(service):
    return "lin_api_" + hashlib.sha256(service.encode()).hexdigest()[:32]


def error_payload(message):
    return {"data": None, "errors": [{"message": message}]}


def newest(nodes):
    return sorted(reversed(nodes), key=lambda n: n["createdAt"], reverse=True)


@functools.cache
def selection(query):
    tokens = re.findall(r"\w+|[{}()]", query)

    def skip(i):
        depth = 0
        while True:
            depth += {"(": 1, ")": -1}.get(tokens[i], 0)
            i += 1
            if depth == 0:
                return i

    def block(i):
        fields, i, last = {}, i + 1, None
        while tokens[i] != "}":
            if tokens[i] == "(":
                i = skip(i)
            elif tokens[i] == "{":
                fields[last], i = block(i)
            else:
                last, i = tokens[i], i + 1
                fields[last] = None
        return fields, i + 1

    i = 0
    while tokens[i] != "{":
        i = skip(i) if tokens[i] == "(" else i + 1
    return block(i)[0]


def project(value, sel, op):
    if sel is None or value is None:
        return value
    if isinstance(value, list):
        return [project(v, sel, op) for v in value]
    if missing := [f for f in sel if f not in value]:
        raise Problem.unknown_field(op, missing[0])
    return {f: project(value[f], s, op) for f, s in sel.items()}


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


def texts():
    return {
        op: BUILT[op] if op in BUILT else getattr(MODULES[op.split(".")[0]], op.split(".")[1])
        for op in OPS
    }


def index(named):
    out = {}
    for name, text in named.items():
        if text in out:
            raise FixtureError.conflicting_operations(out[text], name)
        out[text] = name
    return out


OPS = {
    "linear.M_COMMENT": Operations.handle_comment_create,
    "linear.M_COMMENT_ID": Operations.handle_comment_create_id,
    "linear.Q_COMMENT": Operations.handle_comment_by_id,
    "linear.M_STATE": Operations.handle_set_state,
    "linear.M_SUBSCRIBE": Operations.handle_subscribe,
    "linear.Q_ISSUE_STATE": Operations.handle_issue,
    "linear.Q_USER": Operations.handle_users,
    "linear.Q_TEAM": Operations.handle_teams,
    "linear.Q_TASK_GROUP": Operations.handle_label_group,
    "router.Q_RELATIONS": Operations.handle_issue,
    "router.Q_RECHECK": Operations.handle_issue,
    "router.Q_HISTORY": Operations.handle_issue,
    "router.issues": Operations.handle_board,
    "router.issues+relations": Operations.handle_board,
    "issues.Q_ISSUE": Operations.handle_issue,
    "sessions.M_UPDATE": Operations.handle_comment_update,
    "writeback.M_TITLE": Operations.handle_set_title,
    "writeback.M_ATTACH": Operations.handle_attach,
    "writeback.M_UNARCHIVE": Operations.handle_unarchive,
    "writeback.Q_ATTACHED": Operations.handle_issue,
    "writeback.Q_ID": Operations.handle_issue,
    "promote.Q_HANDOFF": Operations.handle_handoff,
    "promote.Q_DETAIL": Operations.handle_issue,
    "promote.Q_CHILD": Operations.handle_child,
    "promote.M_CREATE": Operations.handle_create,
    "promote.M_RELATE": Operations.handle_relate,
    "prune.Q_ISSUE": Operations.handle_issue,
    "prune.Q_FINISHED": Operations.handle_finished,
    "prune.M_ARCHIVE": Operations.handle_archive,
}

TABLE = index(texts())


class FakeLinear(Operations):
    def __init__(self, harness=None):
        self.harness, self.lock = harness, threading.RLock()
        self.ops = dict(TABLE)
        self.requests, self.failures = [], []
        self.users, self.issues, self.labels, self.projects, self.relations = {}, {}, {}, {}, []

    def user(self, email, name, service=None):
        with self.lock:
            if any(
                u["email"].lower() == email.lower() or (service and u["service"] == service)
                for u in self.users.values()
            ):
                raise FixtureError.duplicate_user(email, service)
            uid = str(uuid.uuid4())
            self.users[uid] = {"id": uid, "email": email, "name": name, "service": service}
            return uid

    def keys(self):
        with self.lock:
            return {
                u["service"]: credential_key(u["service"])
                for u in self.users.values()
                if u["service"]
            }

    def label_group(self, id, name, labels):
        with self.lock:
            self.labels[id] = {"id": id, "name": name, "isGroup": True, "parent": None}
            for lid, lname in labels.items():
                self.labels[lid] = {"id": lid, "name": lname, "isGroup": False, "parent": id}

    def issue(self, title="Issue", *, seed: IssueSeed | None = None):
        seed = seed or IssueSeed()
        with self.lock:
            if (
                seed.assignee is not None
                and seed.assignee not in self.users
                or any(i not in self.labels for i in seed.labels)
            ):
                raise FixtureError.unseeded_access()
            if seed.project:
                self.projects[seed.project["id"]] = seed.project["name"]
            at = stamp(seed.created)
            try:
                return self._add(
                    seed.id or str(uuid.uuid4()),
                    seed.identifier,
                    title=title,
                    description=seed.description,
                    priority=seed.priority,
                    state=IDS[seed.state],
                    assignee=seed.assignee,
                    project=seed.project and dict(seed.project),
                    labels=list(seed.labels),
                    createdAt=at,
                    updatedAt=stamp(seed.updated) if seed.updated else at,
                )
            except Problem as e:
                raise ValueError(str(e)) from None

    def comment(self, issue, user, body, *, at=None):
        with self.lock:
            if user is not None and user not in self.users:
                raise FixtureError.unseeded_user(user)
            return self._comment(self._get(issue), user, body, stamp(at))

    def move(self, issue, to, *, actor, at=None):
        with self.lock:
            self._move(self._get(issue), IDS[to], actor, stamp(at))

    def relate(self, type, issue, related):
        with self.lock:
            a, b = self.find(issue), self.find(related)
            if a is None or b is None:
                raise FixtureError.unseeded_issue(issue if a is None else related)
            self.relations.append((type, a["id"], b["id"]))

    def find(self, ref):
        with self.lock:
            return self.issues.get(ref) or next(
                (i for i in self.issues.values() if i["identifier"] == ref), None
            )

    def fail(self, op, kind, *, account=None, times=1, seconds=3):
        with self.lock:
            if (
                op not in OPS
                or kind not in KINDS
                or (kind == "unsuccessful" and not texts()[op].startswith("mutation"))
                or times < 1
                or (
                    account is not None
                    and not any(
                        u["service"] and u["email"].lower() == account.lower()
                        for u in self.users.values()
                    )
                )
            ):
                raise FixtureError.invalid_failure(op, kind, account, times)
            self.failures.append(InjectedFailure(op, kind, account, times, seconds))

    def answer(self, query, variables, key):
        with self.lock:
            caller = next(
                (
                    u
                    for u in self.users.values()
                    if u["service"] and credential_key(u["service"]) == key
                ),
                None,
            )
            account = caller and caller["email"]
            if not isinstance(query, str) or not isinstance(variables, (dict, type(None))):
                return self._log(
                    Request(None, account, {}, "error"),
                    HttpReply(
                        400,
                        error_payload(
                            "fake: bad request: not a query string and a variables object"
                        ),
                    ),
                )
            variables, op = variables or {}, self.ops.get(query)
            if caller is None:
                return self._log(
                    Request(op or query, None, variables, "unauthorized"),
                    HttpReply(401, error_payload("fake: unauthorized")),
                )
            if op is None:
                return self._log(
                    Request(query, account, variables, "unknown"),
                    HttpReply(200, error_payload("fake: unknown query")),
                )
            failure = self._failure(op, account)
            kind = failure.kind if failure else "ok"
            if kind in ("error", "503"):
                return self._log(
                    Request(op, account, variables, kind),
                    HttpReply(
                        int(kind) if kind == "503" else 200,
                        error_payload(f"fake: {op}: injected {kind}"),
                    ),
                )
            try:
                sel = selection(query)
                if kind == "unsuccessful":
                    top = next(iter(sel))
                    data = {top: {f: False if f == "success" else None for f in sel[top]}}
                else:
                    data = project(OPS[op](self, caller, variables), sel, op)
            except (Problem, KeyError, TypeError, ValueError, LookupError, AttributeError) as e:
                message = (
                    str(e) if isinstance(e, Problem) else f"fake: {op}: {type(e).__name__}: {e}"
                )
                return self._log(
                    Request(op, account, variables, "error"), HttpReply(200, error_payload(message))
                )
            return self._log(
                Request(op, account, variables, kind),
                HttpReply(200, {"data": data}, failure.seconds if kind == "hang" else 0),
            )

    def __call__(self, query, *, timeout=30, service=None, **variables):
        service = service or self.harness
        keys = self.keys()
        if service not in keys:
            raise FakeCredentialError(service)
        status, body, delay = self.answer(query, variables, keys[service])
        if delay:
            time.sleep(min(delay, timeout))
            if delay >= timeout:
                raise linear.Unavailable(linear.operation(query), reason="TimeoutError: timed out")
        if status >= 500:
            raise linear.Unavailable(linear.operation(query), status=status)
        if status != 200:
            raise urllib.error.HTTPError(
                "fake_linear",
                status,
                HTTPStatus(status).phrase,
                None,
                io.BytesIO(json.dumps(body).encode()),
            )
        if body.get("errors"):
            raise LinearError(f"linear api error: {body['errors']}")
        return body["data"]

    def _log(self, request: Request, reply: HttpReply):
        self.requests.append(request)
        return reply.status, reply.payload, reply.delay

    def _failure(self, op, account):
        for f in self.failures:
            if f.op == op and (f.account is None or f.account.lower() == account.lower()):
                f.times -= 1
                if f.times <= 0:
                    self.failures.remove(f)
                return f
        return None


class _Server(ThreadingHTTPServer):
    daemon_threads = False  # server_close joins the handlers: none outlives the case


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/graphql":
            return self._send(404, error_payload("fake: not found"))
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            req = None
        req = req if isinstance(req, dict) else {}
        status, body, delay = self.server.fake.answer(
            req.get("query"), req.get("variables"), self.headers.get("Authorization")
        )
        if delay and self.server.closing.wait(delay):
            return
        self._send(status, body)

    def do_GET(self):
        self._send(405, error_payload("fake: POST only"))

    do_PUT = do_PATCH = do_DELETE = do_GET

    def _send(self, status, body):
        data = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


def serve(case, fake):
    server = _Server(("127.0.0.1", 0), _Handler)
    server.fake, server.closing = fake, threading.Event()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()

    def stop():
        server.closing.set()
        server.shutdown()
        server.server_close()
        thread.join()

    case.addCleanup(stop)
    return f"http://127.0.0.1:{server.server_address[1]}/graphql"


def seam(case, fake, url):
    fd, path = tempfile.mkstemp(prefix="fake-linear-", suffix=".json")
    case.addCleanup(os.remove, path)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url, "keys": fake.keys()}, f)
    return path
