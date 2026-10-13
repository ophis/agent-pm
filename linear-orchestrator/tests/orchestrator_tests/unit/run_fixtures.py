"""Run-side fixtures: ids, the config files config.load() reads (in the test's HOME), a Linear fake
recording each call's identity, and the orchestrator log's readers.
"""

import json
import os
import shutil
import tempfile
from datetime import datetime
from unittest import mock

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import issues, linear, sessions, writeback
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import HEADER, STATES, role

ID, UUID = "TASK-7", "11111111-2222-4333-8444-555555555555"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
PROJECT = "121166b1-191a-4461-bec4-42f1c2dc0ddd"
ENGINEER, RESEARCHER = "engineer@agents.test", "researcher@agents.test"
HARNESS, KEY = "linear-api-key", "linear-api-key-engineer"
SESSION = linear.client_id(SID, "session")
NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")
URL = f"https://linear.app/t/issue/{ID}"
USER_NOTE = {
    "body": "Use SQLite.",
    "createdAt": "2026-09-02T00:00:00.000Z",
    "user": {"email": "ME@x.com", "name": "Me", "isMe": False},
}
CORE_LOCAL = """[roles.researcher.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Research/"

[roles.pm.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Product Design/"
"""


def local(work):
    return (
        HEADER
        + 'human_members = ["Me@X.com"]\n'
        + f'work_dir = "{work}"\n'
        + role("researcher")
        + role("pm")
        + role("engineer")
        + f'[project_repos]\n"{PROJECT}" = "ophis/agent-pm"\n'
    )


def logged(logs=None):
    try:
        with open(
            os.path.join(logs or linear.LOGS_DIR, "orchestrator.jsonl"), encoding="utf-8"
        ) as f:
            lines = [json.loads(line) for line in f]
    except FileNotFoundError:
        return []
    for d in lines:
        assert (
            list(d)[:3] == ["ts", "src", "kind"]
            and datetime.fromisoformat(d["ts"]).utcoffset() is not None
        ), d
    return lines


def show(event):
    fields = [
        f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
        for k, v in event.items()
        if k not in ("ts", "src", "kind", "issue")
    ]
    return " ".join([event["kind"], *([event["issue"]] if "issue" in event else []), *fields])


def node(title="ENG: Session registry", description="Add a session registry.", comments=()):
    return {
        "id": UUID,
        "identifier": ID,
        "url": URL,
        "title": title,
        "description": description,
        "createdAt": "2026-09-01T00:00:00.000Z",
        "project": {"id": PROJECT},
        "comments": {"nodes": list(comments)},
        "attachments": {"nodes": []},
        "relations": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }


NAMES = {
    issues.Q_ISSUE: "issue",
    linear.M_COMMENT: "post",
    sessions.M_UPDATE: "update",
    linear.M_COMMENT_ID: "comment",
    linear.Q_COMMENT: "find",
    writeback.Q_ATTACHED: "read",
    linear.Q_ISSUE_STATE: "reread",
    linear.M_STATE: "state",
    linear.M_SUBSCRIBE: "subscribe",
    writeback.M_ATTACH: "attach",
    writeback.M_TITLE: "title",
}
FIELDS = {
    "update": "commentUpdate",
    "comment": "commentCreate",
    "post": "commentCreate",
    "state": "issueUpdate",
    "title": "issueUpdate",
    "subscribe": "issueSubscribe",
    "attach": "attachmentLinkURL",
}


class Gql:
    """Linear: calls recorded as (name, service, vars); comments kept by client id, the issue as its
    UUID; `issue`: the issue read's node; `fail(name, vars)` returns an exception to raise.
    """

    def __init__(self, fail=lambda name, v: None, issue=None):
        self.fail, self.issue, self.calls, self.timeouts, self.comments = fail, issue, [], [], {}

    def __call__(self, query, *, service, timeout=30, **v):
        name = NAMES[query]
        self.calls.append((name, service, v))
        self.timeouts.append(timeout)
        if (e := self.fail(name, v)) is not None:
            raise e
        if name == "issue":
            return {"issue": self.issue}
        if name == "find":
            if v["c"] not in self.comments:
                raise NOT_FOUND
            body, issue = self.comments[v["c"]]
            return {
                "comment": {
                    "id": v["c"],
                    "body": body,
                    "issue": {"id": issue},
                    "user": {"isMe": True},
                }
            }
        if name == "read":
            return {"issue": {"attachments": {"nodes": []}}}
        if name == "reread":
            return {"issue": {"state": {"id": STATES["in_progress"]}}}
        if name == "update":
            if v["c"] not in self.comments:
                raise NOT_FOUND
            self.comments[v["c"]] = (v["b"], self.comments[v["c"]][1])
        elif name == "comment":
            self.comments[v["c"]] = (v["b"], UUID if v["i"] == ID else v["i"])
        return {FIELDS[name]: {"success": True}}


class Base(Case):
    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.local = os.path.join(self.agent_pm, "config.local.toml")
        self.write_local(local(self.work))
        self.rd = os.path.join(self.work, "work", ID)
        self.logs = os.path.join(self.work, "logs")
        self.runs = os.path.join(self.logs, "runs.jsonl")
        linear.use_logs(None)
        for p in (mock.patch.dict(os.environ),):
            p.start()
            self.addCleanup(p.stop)
        self.gql = Gql()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def write_local(self, text):
        """~/.agent-pm/config.local.toml: `text`, then CORE_LOCAL (the delegate's keys)."""
        self.write(self.local, text + "\n" + CORE_LOCAL)

    def said(self, src=None):
        return [
            show(e) if src else f"{e['src']} {show(e)}"
            for e in logged(self.logs)
            if src in (None, e["src"])
        ]
