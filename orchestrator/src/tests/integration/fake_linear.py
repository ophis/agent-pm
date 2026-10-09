"""A fake Linear for orchestrator integration tests, stdlib only: FakeLinear, a thread-safe model of one team's board
that answers the orchestrator's own GraphQL texts; serve() puts it on 127.0.0.1 over HTTP and seam() writes the
AGENT_PM_LINEAR file (linear.py's docstring) pointing linear_gql at it.

Dispatch: by exact query text (texts()), each an op named <module>.<CONSTANT>, plus router.issues and
router.issues+relations (router.q_issues() and q_issues(RELATIONS)). An unknown text: a GraphQL error `fake: unknown
query`. Each answer is cut to the query's selection set (selection(), project()): a field not selected is missing, as in
Linear; a selected field the model lacks is a GraphQL error `fake: <op> has no field <name>`.

Callers: a user seeded with a service has a key derived from it (keys()); the caller is the user whose key a request
carries (Authorization, no "Bearer"); a missing or unknown key gets HTTP 401 with a GraphQL error. isMe, comment authors
and history actors are the caller.

Semantics, as Linear's for what the orchestrator reads: the team board_ids.TEAM (key TASK, board_ids.STATES named as
config.STATES, typed as TYPES); issue(id:) by uuid or identifier, archived too, a missing one a GraphQL error; lists skip
archived issues unless includeArchived, in creation order, paged by position cursors; history and comments newest first;
times in Linear's form (stamp()), now from the real clock.
"""
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
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hermetic  # noqa: F401  (first: config reads HOME at import)
from board_ids import STATES, TEAM
import config
import issues
import linear
import promote
import prune
import router
import sessions
import writeback

KEY = "TASK"
TYPES = {"todo": "unstarted", "in_progress": "started", "in_review": "started", "handoff": "started",
         "done": "completed", "canceled": "canceled"}
NAMES = {i: k for k, i in STATES.items()}
KINDS = ("error", "unsuccessful", "503", "hang")
MODULES = {m.__name__: m for m in (linear, router, issues, sessions, writeback, promote, prune)}
BUILT = {"router.issues": router.q_issues(), "router.issues+relations": router.q_issues(router.RELATIONS)}
CREATE = {"id", "teamId", "projectId", "assigneeId", "stateId", "priority", "title", "description"}
COMPARATORS = {"eq": lambda x, a: x == a, "in": lambda x, a: x in a, "null": lambda x, a: (x is None) == a}


class Problem(Exception):
    """An answer of HTTP 200 with a GraphQL error, the message."""


@dataclass
class Request:
    """A request-log entry. op: the op, or the query text when unknown; account: the caller's email, None for a bad key;
    result: ok, a failure kind (error also for the model's own GraphQL error), unknown or unauthorized."""
    op: str
    account: str | None
    variables: dict
    result: str


@dataclass
class _Failure:
    op: str
    kind: str
    account: str | None
    times: int
    seconds: float


def stamp(t=None):
    """t (a datetime, an ISO 8601 string; default now) in Linear's form, 2026-10-09T07:30:41.062Z."""
    if t is None:
        t = datetime.now(timezone.utc)
    elif isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    t = t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


@functools.cache
def selection(query):
    """The query's selection set as {field: its selection or None}; the operation header and argument lists skipped."""
    tokens = re.findall(r"\w+|[{}()]", query)

    def skip(i):  # tokens[i] == "(": the index after its ")"
        depth = 0
        while True:
            depth += {"(": 1, ")": -1}.get(tokens[i], 0)
            i += 1
            if depth == 0:
                return i

    def block(i):  # tokens[i] == "{": (its fields, the index after its "}")
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
    """value cut to sel (selection()'s shape), a list element-wise, None as is; Problem on a selected field it lacks."""
    if sel is None or value is None:
        return value
    if isinstance(value, list):
        return [project(v, sel, op) for v in value]
    if missing := [f for f in sel if f not in value]:
        raise Problem(f"fake: {op} has no field {missing[0]}")
    return {f: project(value[f], s, op) for f, s in sel.items()}


def texts():
    """{op: its query text}, for each op of OPS."""
    return {op: BUILT[op] if op in BUILT else getattr(MODULES[op.split(".")[0]], op.split(".")[1]) for op in OPS}


def index(named):
    """{text: name} of {name: text}; ValueError naming both names of a shared text."""
    out = {}
    for name, text in named.items():
        if text in out:
            raise ValueError(f"one query text, two ops: {out[text]}, {name}")
        out[text] = name
    return out


def _key(service):
    return "lin_api_" + hashlib.sha256(service.encode()).hexdigest()[:32]


def _errors(message):
    return {"data": None, "errors": [{"message": message}]}


def _newest(nodes):
    """Newest createdAt first; a tie, the later added first."""
    return sorted(reversed(nodes), key=lambda n: n["createdAt"], reverse=True)


class FakeLinear:
    """The model, its answers, failures and request log, under one lock. harness: the service the in-process call
    uses when given none (linear_gql's default is the config's harness_key). Records are the model's own dicts: an issue
    {id, identifier, url, title, description, priority, createdAt, updatedAt, state (id), project ({id, name} or None),
    assignee (user id or None), labels (ids), comments [{id, body, createdAt, user (id or None)}], attachments [{url,
    title}], subscribers (emails), history [{createdAt, actorId, fromStateId, toStateId}], archived}; relations
    (type, issue id, related issue id)."""

    def __init__(self, harness=None):
        self.harness, self.lock = harness, threading.RLock()
        self.ops = dict(TABLE)  # {text: op}; a test may map another text to an op
        self.requests, self.failures = [], []
        self.users, self.issues, self.labels, self.projects, self.relations = {}, {}, {}, {}, []

    # Seeding: a time is stamp()'s input, default now.

    def user(self, email, name, service=None):
        """Adds a user; returns its id. With service, it has a key (keys())."""
        with self.lock:
            if any(u["email"].lower() == email.lower() or (service and u["service"] == service)
                   for u in self.users.values()):
                raise ValueError(f"user {email} or service {service} seeded twice")
            uid = str(uuid.uuid4())
            self.users[uid] = {"id": uid, "email": email, "name": name, "service": service}
            return uid

    def keys(self):
        """{service: key} of the users with a service, now; a key is derived from its service."""
        with self.lock:
            return {u["service"]: _key(u["service"]) for u in self.users.values() if u["service"]}

    def label_group(self, id, name, labels):
        """Adds label group id and its labels, {id: name}."""
        with self.lock:
            self.labels[id] = {"id": id, "name": name, "isGroup": True, "parent": None}
            for lid, lname in labels.items():
                self.labels[lid] = {"id": lid, "name": lname, "isGroup": False, "parent": id}

    def issue(self, title="Issue", *, identifier=None, id=None, state="todo", assignee=None, project=None,
              description="", priority=0, labels=(), created=None, updated=None):
        """Adds an issue of the team; returns its record. identifier TASK-<n> (default the next number); id a uuid
        (default a new one); state a board_ids.STATES key; assignee a user id; project {id, name}; labels label ids;
        updated defaults to created."""
        with self.lock:
            if (assignee is not None and assignee not in self.users) or any(i not in self.labels for i in labels):
                raise ValueError("assignee or a label not seeded")
            if project:
                self.projects[project["id"]] = project["name"]
            at = stamp(created)
            return self._add(id or str(uuid.uuid4()), identifier, title=title, description=description,
                             priority=priority, state=STATES[state], assignee=assignee, project=project and dict(project),
                             labels=list(labels), createdAt=at, updatedAt=stamp(updated) if updated else at)

    def comment(self, issue, user, body, *, at=None):
        """Adds a comment by user (an id; None: an integration's); returns its id."""
        with self.lock:
            if user is not None and user not in self.users:
                raise ValueError(f"user {user} not seeded")
            return self._comment(self._get(issue), user, body, stamp(at))

    def move(self, issue, to, *, actor, at=None):
        """Moves the issue to state `to` (a board_ids.STATES key) as actor (a user id): a history node."""
        with self.lock:
            self._move(self._get(issue), STATES[to], actor, stamp(at))

    def find(self, ref):
        """The issue record of a uuid or identifier, else None."""
        with self.lock:
            return self.issues.get(ref) or next((i for i in self.issues.values() if i["identifier"] == ref), None)

    def fail(self, op, kind, *, account=None, times=1, seconds=3):
        """The next `times` calls of op (with account's key only, an email, when given) fail as kind: error (GraphQL
        errors: linear_gql raises SystemExit), unsuccessful (a mutation's success: false: linear.call raises
        RuntimeError), 503 (urllib.error.HTTPError), all three changing nothing; hang (applied, answered after seconds:
        a shorter client timeout raises TimeoutError). ValueError for an unknown op or kind, or unsuccessful on a query."""
        if op not in OPS or kind not in KINDS or (kind == "unsuccessful" and not texts()[op].startswith("mutation")):
            raise ValueError(f"fail({op!r}, {kind!r}): an unknown op or kind, or unsuccessful on a query")
        with self.lock:
            self.failures.append(_Failure(op, kind, account, times, seconds))

    def answer(self, query, variables, key):
        """(HTTP status, JSON body, delay in seconds) of one request, logged; serve() and __call__ both send it."""
        variables = variables or {}
        with self.lock:
            op, caller = self.ops.get(query), next((u for u in self.users.values()
                                                    if u["service"] and _key(u["service"]) == key), None)
            if caller is None:
                return self._log(op or query, None, variables, "unauthorized", 401, _errors("fake: unauthorized"))
            account = caller["email"]
            if op is None:
                return self._log(query, account, variables, "unknown", 200, _errors("fake: unknown query"))
            failure = self._failure(op, account)
            kind = failure.kind if failure else "ok"
            if kind in ("error", "503"):
                return self._log(op, account, variables, kind, int(kind) if kind == "503" else 200,
                                 _errors(f"fake: {op}: injected {kind}"))
            sel = selection(query)
            if kind == "unsuccessful":
                top = next(iter(sel))
                return self._log(op, account, variables, kind, 200,
                                 {"data": {top: {f: False if f == "success" else None for f in sel[top]}}})
            try:
                data = project(OPS[op](self, caller, variables), sel, op)
            except Problem as e:
                return self._log(op, account, variables, "error", 200, _errors(str(e)))
            return self._log(op, account, variables, kind, 200, {"data": data}, failure.seconds if failure else 0)

    def __call__(self, query, *, timeout=30, service=None, **variables):
        """linear_gql in process: answer() raised as linear_gql raises it."""
        service = service or self.harness
        keys = self.keys()
        if service not in keys:
            raise SystemExit(f"{linear.SEAM}: no key for {service}")
        status, body, delay = self.answer(query, variables, keys[service])
        if delay:
            time.sleep(min(delay, timeout))
            if delay >= timeout:
                raise TimeoutError("timed out")
        if status != 200:
            raise urllib.error.HTTPError("fake_linear", status, HTTPStatus(status).phrase, None,
                                         io.BytesIO(json.dumps(body).encode()))
        if body.get("errors"):
            raise SystemExit(f"linear api error: {body['errors']}")
        return body["data"]

    def _log(self, op, account, variables, result, status, body, delay=0):
        self.requests.append(Request(op, account, variables, result))
        return status, body, delay

    def _failure(self, op, account):
        """The first failure of op for account, its times counted down, else None."""
        for f in self.failures:
            if f.op == op and (f.account is None or f.account.lower() == account.lower()):
                f.times -= 1
                if f.times <= 0:
                    self.failures.remove(f)
                return f
        return None

    def _get(self, ref):
        if (rec := self.find(ref)) is None:
            raise Problem("Entity not found: Issue")
        return rec

    def _add(self, id, identifier, **fields):
        numbers = [int(i["identifier"].split("-")[1]) for i in self.issues.values()]
        identifier = identifier or f"{KEY}-{max(numbers, default=0) + 1}"
        if self.find(id) or self.find(identifier) or not re.fullmatch(rf"{KEY}-\d+", identifier):
            raise Problem(f"fake: issue id {id} or identifier {identifier} in use, or not {KEY}-<n>")
        self.issues[id] = {"id": id, "identifier": identifier, "url": f"https://linear.app/fake/issue/{identifier}",
                           **fields, "comments": [], "attachments": [], "subscribers": [], "history": [],
                           "archived": False}
        return self.issues[id]

    def _comment(self, rec, user, body, at):
        cid = str(uuid.uuid4())
        rec["comments"].append({"id": cid, "body": body, "createdAt": at, "user": user})
        return cid

    def _move(self, rec, state, actor, at):
        if state != rec["state"]:
            rec["history"].append({"createdAt": at, "actorId": actor, "fromStateId": rec["state"], "toStateId": state})
            rec["state"], rec["updatedAt"] = state, at

    # Views: Linear's objects, every field the orchestrator selects.

    def _state(self, sid):
        return {"id": sid, "name": config.STATES[NAMES[sid]], "type": TYPES[NAMES[sid]]}

    def _team(self):
        return {"id": TEAM, "name": "Team", "key": KEY, "states": {"nodes": [self._state(i) for i in STATES.values()]}}

    def _person(self, uid, caller):
        u = self.users.get(uid) if uid else None
        return u and {"id": u["id"], "email": u["email"], "name": u["name"], "isMe": u["id"] == caller["id"]}

    def _label(self, lid):
        parent = self.labels[lid]["parent"]
        return {**{k: self.labels[lid][k] for k in ("id", "name", "isGroup")}, "parent": parent and {"id": parent}}

    def _brief(self, iid):
        rec = self.issues[iid]
        return {**{k: rec[k] for k in ("id", "identifier", "title", "url")}, "state": self._state(rec["state"])}

    def _view(self, rec, caller):
        out = {k: rec[k] for k in ("id", "identifier", "url", "title", "description", "priority", "createdAt",
                                   "updatedAt", "project")}
        return {**out, "state": self._state(rec["state"]), "team": self._team(),
                "assignee": self._person(rec["assignee"], caller),
                "labels": {"nodes": [self._label(i) for i in rec["labels"]]},
                "comments": {"nodes": _newest([{**c, "user": self._person(c["user"], caller)} for c in rec["comments"]])},
                "attachments": {"nodes": rec["attachments"]},
                "history": {"nodes": _newest(rec["history"])},
                "relations": {"nodes": [{"type": t, "issue": self._brief(a), "relatedIssue": self._brief(b)}
                                        for t, a, b in self.relations if a == rec["id"]]},
                "inverseRelations": {"nodes": [{"type": t, "issue": self._brief(a), "relatedIssue": self._brief(b)}
                                               for t, a, b in self.relations if b == rec["id"]]}}

    def _list(self, caller, flt, first, after=None, archived=False):
        """The issues matching flt (archived ones too when archived), first of them after the cursor `after`."""
        found = [i for i in self.issues.values() if (archived or not i["archived"]) and self._match(i, flt)]
        start = int(after or 0)
        page = found[start:start + first]
        return {"issues": {"nodes": [self._view(i, caller) for i in page],
                           "pageInfo": {"hasNextPage": start + first < len(found),
                                        "endCursor": str(start + len(page)) if page else None}}}

    def _match(self, rec, flt):
        """rec meets flt, the filters the orchestrator sends: id, team, state, assignee, project; eq, in, null."""
        values = {"id": rec["id"], "team": TEAM, "state": rec["state"], "assignee": rec["assignee"],
                  "project": rec["project"] and rec["project"]["id"]}
        for field, cond in flt.items():
            if field not in values:
                raise Problem(f"fake: no issue filter {field}")
            for c, arg in (cond["id"] if field != "id" and "id" in cond else cond).items():
                if c not in COMPARATORS:
                    raise Problem(f"fake: no comparator {c}")
                if not COMPARATORS[c](values[field], arg):
                    return False
        return True

    # Ops: (caller, variables) -> the answer's data before projection.

    def _issue(self, caller, v):
        return {"issue": self._view(self._get(v["i"]), caller)}

    def _find(self, caller, v):
        nodes = self._view(self._get(v["i"]), caller)["comments"]["nodes"]
        return {"issue": {"comments": {"nodes": [c for c in nodes if c["user"] and c["user"]["isMe"]
                                                 and c["body"].startswith(v["p"])]}}}

    def _users(self, caller, v):
        return {"users": {"nodes": [self._person(i, caller) for i, u in self.users.items()
                                    if u["email"].lower() == v["e"].lower()]}}

    def _teams(self, caller, v):
        return {"teams": {"nodes": [self._team()] if v.get("t") == TEAM else []}}

    def _label_group(self, caller, v):
        if v["i"] not in self.labels:
            raise Problem("Entity not found: IssueLabel")
        children = [self._label(i) for i, label in self.labels.items() if label["parent"] == v["i"]]
        return {"issueLabel": {**self._label(v["i"]), "children": {"nodes": children}}}

    def _board(self, caller, v):
        return self._list(caller, v.get("f") or {}, 100)

    def _handoff(self, caller, v):
        return self._list(caller, {"team": {"id": {"eq": v.get("t")}}, "state": {"id": {"eq": v.get("s")}},
                                   "project": {"null": False}, "assignee": {"id": {"in": v.get("a")}}}, 100)

    def _child(self, caller, v):
        return self._list(caller, {"id": {"eq": v["c"]}}, 50, archived=True)

    def _finished(self, caller, v):
        return self._list(caller, {"team": {"id": {"eq": v.get("t")}}, "state": {"id": {"in": v.get("s")}},
                                   "assignee": {"id": {"in": v.get("a")}}}, 50, v.get("c"))

    def _comment_create(self, caller, v):
        self._comment(self._get(v["i"]), caller["id"], v["b"], stamp())
        return {"commentCreate": {"success": True}}

    def _comment_update(self, caller, v):
        found = next((c for i in self.issues.values() for c in i["comments"] if c["id"] == v["c"]), None)
        if found is None:
            raise Problem("Entity not found: Comment")
        if found["user"] != caller["id"]:
            raise Problem("fake: commentUpdate: only its author edits a comment")
        found["body"] = v["b"]
        return {"commentUpdate": {"success": True}}

    def _set_state(self, caller, v):
        if v["s"] not in NAMES:
            raise Problem("Entity not found: WorkflowState")
        self._move(self._get(v["i"]), v["s"], caller["id"], stamp())
        return {"issueUpdate": {"success": True}}

    def _set_title(self, caller, v):
        rec = self._get(v["i"])
        rec["title"], rec["updatedAt"] = v["t"], stamp()
        return {"issueUpdate": {"success": True}}

    def _subscribe(self, caller, v):
        rec = self._get(v["i"])
        user = next((u for u in self.users.values() if u["email"].lower() == v["e"].lower()), None)
        if user is None:
            raise Problem("Entity not found: User")
        if user["email"] not in rec["subscribers"]:
            rec["subscribers"].append(user["email"])
        return {"issueSubscribe": {"success": True}}

    def _attach(self, caller, v):
        rec = self._get(v["i"])
        if all(a["url"] != v["u"] for a in rec["attachments"]):
            rec["attachments"].append({"url": v["u"], "title": v.get("t")})
        return {"attachmentLinkURL": {"success": True}}

    def _archive(self, caller, v):
        self._get(v["i"])["archived"] = True
        return {"issueArchive": {"success": True}}

    def _unarchive(self, caller, v):
        self._get(v["i"])["archived"] = False
        return {"issueUnarchive": {"success": True}}

    def _create(self, caller, v):
        new, now = v["in"], stamp()
        pid, uid, sid = new.get("projectId"), new.get("assigneeId"), new.get("stateId", STATES["todo"])
        if extra := sorted(set(new) - CREATE):
            raise Problem(f"fake: issueCreate has no input {extra[0]}")
        if (new.get("teamId") != TEAM or (pid and pid not in self.projects) or (uid and uid not in self.users)
                or sid not in NAMES):
            raise Problem("Entity not found: Team, Project, User or WorkflowState")
        rec = self._add(new.get("id") or str(uuid.uuid4()), None, title=new.get("title", ""),
                        description=new.get("description"), priority=new.get("priority", 0), state=sid, assignee=uid,
                        project=pid and {"id": pid, "name": self.projects[pid]}, labels=[], createdAt=now, updatedAt=now)
        return {"issueCreate": {"success": True, "issue": self._view(rec, caller)}}

    def _relate(self, caller, v):
        new = v["in"]
        self.relations.append((new["type"], self._get(new["issueId"])["id"], self._get(new["relatedIssueId"])["id"]))
        return {"issueRelationCreate": {"success": True}}


OPS = {
    "linear.M_COMMENT": FakeLinear._comment_create,
    "linear.M_STATE": FakeLinear._set_state,
    "linear.M_SUBSCRIBE": FakeLinear._subscribe,
    "linear.Q_ISSUE_STATE": FakeLinear._issue,
    "linear.Q_USER": FakeLinear._users,
    "linear.Q_TEAM": FakeLinear._teams,
    "linear.Q_TASK_GROUP": FakeLinear._label_group,
    "router.Q_RELATIONS": FakeLinear._issue,
    "router.Q_RECHECK": FakeLinear._issue,
    "router.Q_HISTORY": FakeLinear._issue,
    "router.issues": FakeLinear._board,
    "router.issues+relations": FakeLinear._board,
    "issues.Q_ISSUE": FakeLinear._issue,
    "sessions.Q_FIND": FakeLinear._find,
    "sessions.M_UPDATE": FakeLinear._comment_update,
    "writeback.M_TITLE": FakeLinear._set_title,
    "writeback.M_ATTACH": FakeLinear._attach,
    "writeback.M_UNARCHIVE": FakeLinear._unarchive,
    "writeback.Q_ATTACHED": FakeLinear._issue,
    "writeback.Q_ID": FakeLinear._issue,
    "promote.Q_HANDOFF": FakeLinear._handoff,
    "promote.Q_DETAIL": FakeLinear._issue,
    "promote.Q_CHILD": FakeLinear._child,
    "promote.M_CREATE": FakeLinear._create,
    "promote.M_RELATE": FakeLinear._relate,
    "prune.Q_ISSUE": FakeLinear._issue,
    "prune.Q_FINISHED": FakeLinear._finished,
    "prune.M_ARCHIVE": FakeLinear._archive,
}
TABLE = index(texts())


class _Server(ThreadingHTTPServer):
    daemon_threads = False  # server_close joins the handlers: none outlives the case


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/graphql":
            return self._send(404, _errors("fake: not found"))
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        status, body, delay = self.server.fake.answer(req.get("query"), req.get("variables"),
                                                      self.headers.get("Authorization"))
        if delay and self.server.closing.wait(delay):
            return
        self._send(status, body)

    def do_GET(self):
        self._send(405, _errors("fake: POST only"))

    do_PUT = do_PATCH = do_DELETE = do_GET

    def _send(self, status, body):
        data = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # the client gave up (its timeout)
            pass

    def log_message(self, format, *args):  # test output stays pristine
        pass


def serve(case, fake):
    """Serves fake at 127.0.0.1 (POST /graphql only) until case's cleanup; returns the URL."""
    server = _Server(("127.0.0.1", 0), _Handler)
    server.fake, server.closing = fake, threading.Event()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    def stop():
        server.closing.set()  # a hang answers nothing
        server.shutdown()
        server.server_close()
        thread.join()
    case.addCleanup(stop)
    return f"http://127.0.0.1:{server.server_address[1]}/graphql"


def seam(case, fake, url):
    """Writes an AGENT_PM_LINEAR file of url and fake's keys now (removed in case's cleanup); returns its path."""
    fd, path = tempfile.mkstemp(prefix="fake-linear-", suffix=".json")
    case.addCleanup(os.remove, path)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url, "keys": fake.keys()}, f)
    return path
