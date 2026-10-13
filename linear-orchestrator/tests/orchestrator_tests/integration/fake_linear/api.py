import io
import json
import threading
import time
import urllib.error
import uuid
from http import HTTPStatus

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.integration.fake_linear.data import IDS, KINDS, _errors, _key, stamp
from orchestrator_tests.integration.fake_linear.operations import Operations
from orchestrator_tests.integration.fake_linear.projection import project, selection
from orchestrator_tests.integration.fake_linear.registry import OPS, TABLE, texts
from orchestrator_tests.integration.fake_linear.types import Request, _Failure
from orchestrator_tests.integration.fixture_errors import FakeCredentialError, FixtureError, Problem


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
            return {u["service"]: _key(u["service"]) for u in self.users.values() if u["service"]}

    def label_group(self, id, name, labels):
        with self.lock:
            self.labels[id] = {"id": id, "name": name, "isGroup": True, "parent": None}
            for lid, lname in labels.items():
                self.labels[lid] = {"id": lid, "name": lname, "isGroup": False, "parent": id}

    def issue(
        self,
        title="Issue",
        *,
        identifier=None,
        id=None,
        state="todo",
        assignee=None,
        project=None,
        description="",
        priority=0,
        labels=(),
        created=None,
        updated=None,
    ):
        with self.lock:
            if (assignee is not None and assignee not in self.users) or any(
                i not in self.labels for i in labels
            ):
                raise FixtureError.unseeded_access()
            if project:
                self.projects[project["id"]] = project["name"]
            at = stamp(created)
            try:
                return self._add(
                    id or str(uuid.uuid4()),
                    identifier,
                    title=title,
                    description=description,
                    priority=priority,
                    state=IDS[state],
                    assignee=assignee,
                    project=project and dict(project),
                    labels=list(labels),
                    createdAt=at,
                    updatedAt=stamp(updated) if updated else at,
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
            self.failures.append(_Failure(op, kind, account, times, seconds))

    def answer(self, query, variables, key):
        with self.lock:
            caller = next(
                (u for u in self.users.values() if u["service"] and _key(u["service"]) == key), None
            )
            account = caller and caller["email"]
            if not isinstance(query, str) or not isinstance(variables, (dict, type(None))):
                return self._log(
                    None,
                    account,
                    {},
                    "error",
                    400,
                    _errors("fake: bad request: not a query string and a variables object"),
                )
            variables, op = variables or {}, self.ops.get(query)
            if caller is None:
                return self._log(
                    op or query, None, variables, "unauthorized", 401, _errors("fake: unauthorized")
                )
            if op is None:
                return self._log(
                    query, account, variables, "unknown", 200, _errors("fake: unknown query")
                )
            failure = self._failure(op, account)
            kind = failure.kind if failure else "ok"
            if kind in ("error", "503"):
                return self._log(
                    op,
                    account,
                    variables,
                    kind,
                    int(kind) if kind == "503" else 200,
                    _errors(f"fake: {op}: injected {kind}"),
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
                return self._log(op, account, variables, "error", 200, _errors(message))
            return self._log(
                op,
                account,
                variables,
                kind,
                200,
                {"data": data},
                failure.seconds if kind == "hang" else 0,
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
            raise LinearError.api(body["errors"])
        return body["data"]

    def _log(self, op, account, variables, result, status, body, delay=0):
        self.requests.append(Request(op, account, variables, result))
        return status, body, delay

    def _failure(self, op, account):
        for f in self.failures:
            if f.op == op and (f.account is None or f.account.lower() == account.lower()):
                f.times -= 1
                if f.times <= 0:
                    self.failures.remove(f)
                return f
        return None
