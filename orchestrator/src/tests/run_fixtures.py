"""Fixtures of an agent run shared by router_test (the outer) and run_test (the inner): a temp root linking the repo's core,
a fake Linear (Gql) and a fake gh/git (Run)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
from board_ids import HEADER, STATES, role  # noqa: E402
import config  # noqa: E402
import inputs  # noqa: E402
import issues  # noqa: E402
import linear  # noqa: E402
import sessions  # noqa: E402
import writeback  # noqa: E402

ID, UUID = "TASK-7", "11111111-2222-4333-8444-555555555555"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
PROJECT = "121166b1-191a-4461-bec4-42f1c2dc0ddd"
URL = f"https://linear.app/t/issue/{ID}"
ENGINEER, RESEARCHER, PM = "engineer@agents.test", "researcher@agents.test", "pm@agents.test"
KEY = "linear-api-key-engineer"
CONFIG = (HEADER + 'human_members = ["Me@X.com"]\n' + role("researcher") + role("pm") + role("engineer")
          + f'[project_repos]\n"{PROJECT}" = "ophis/agent-pm"\n'
          + '[core.roles.researcher.tasks.deep-research]\ngate = "python3 {{root}}/orchestrator/src/router.py --brake"\n')
TS = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
LIST = ["tmux", "list-sessions", "-F", "#{session_name}"]
USER_NOTE = {"body": "Use SQLite.", "createdAt": "2026-09-02T00:00:00.000Z",
             "user": {"email": "ME@x.com", "name": "Me", "isMe": False}}
LS_REMOTE = ("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "ls-remote", "--heads",
             "https://github.com/Ophis/Agent-PM.git", "TASK-7-*")
LISTING = ("gh", "api", "repos/ophis/private_docs/contents/Research?ref=main")
DESIGN_LISTING = ("gh", "api", "repos/ophis/private_docs/contents/Product%20Design?ref=main")
INPUT = f"""Reference: TASK-7
Title: TASK-7: Session registry
Repo: Ophis/Agent-PM
Checkout: TASK-7
Branch: TASK-7-session-registry
Links: {URL}

{inputs.PRECEDENCE["build"]}

## The user's requirements since the last build

Me, 2026-09-02T00:00:00.000Z:
Use SQLite.

## The user's instructions

ENG: Session registry

Add a session registry.

## PRD

None linked."""


def res(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


API = res(json.dumps({"full_name": "Ophis/Agent-PM", "default_branch": "main", "permissions": {"push": True}}))
ENG_RUN = [(("gh", "api", "repos/ophis/agent-pm"), API), (LS_REMOTE[:3], res())]


def node(title="ENG: Session registry", description="Add a session registry.", comments=()):
    return {"id": UUID, "identifier": ID, "url": URL, "title": title, "description": description,
            "createdAt": "2026-09-01T00:00:00.000Z", "state": {"id": STATES["in_progress"]}, "project": {"id": PROJECT},
            "comments": {"nodes": list(comments)}, "attachments": {"nodes": []}, "relations": {"nodes": []},
            "inverseRelations": {"nodes": []}}


NAMES = {issues.Q_ISSUE: "issue", sessions.Q_FIND: "find", linear.M_COMMENT: "comment", sessions.M_UPDATE: "update",
         writeback.Q_ATTACHED: "read", linear.Q_ISSUE_STATE: "reread", linear.M_STATE: "state",
         linear.M_SUBSCRIBE: "subscribe", writeback.M_ATTACH: "attach", writeback.Q_ID: "id"}
FIELDS = {"comment": "commentCreate", "update": "commentUpdate", "state": "issueUpdate", "subscribe": "issueSubscribe",
          "attach": "attachmentLinkURL"}


class Gql:
    """Fake Linear recording (name, service, variables) and each call's timeout; fail(name, v) → an exception to raise."""
    def __init__(self, issue, fail=lambda name, v: None):
        self.issue, self.fail, self.calls, self.timeouts = issue, fail, [], []

    def __call__(self, query, *, timeout=30, service=None, **v):
        name = NAMES[query]
        self.calls.append((name, service, v))
        self.timeouts.append(timeout)
        if (e := self.fail(name, v)) is not None:
            raise e
        if name == "issue":
            return {"issue": self.issue}
        if name == "find":
            return {"issue": {"comments": {"nodes": []}}}
        if name == "read":
            return {"issue": {"attachments": {"nodes": []}}}
        if name == "reread":
            return {"issue": {"state": {"id": STATES["in_progress"]}}}
        if name == "id":
            return {"issue": None}
        return {FIELDS[name]: {"success": True}}


class Run:
    """Fake gh/git: (argv prefix, result or exception) pairs; records (argv, timeout)."""
    def __init__(self, table):
        self.table, self.calls = list(table), []

    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        for prefix, r in self.table:
            if tuple(argv[:len(prefix)]) == prefix:
                if isinstance(r, BaseException):
                    raise r
                return r
        raise AssertionError(f"unexpected {argv}")


def forwarded(assignee=ENGINEER, task="build", mode="new", sid=SID):
    """SHARED as the outer passes them to the inner."""
    return [f"--issue={ID}", f"--project={PROJECT}", f"--assignee={assignee}", f"--sid={sid}", f"--task={task}", f"--mode={mode}"]


class Base(unittest.TestCase):
    """A temp root ("my root") linking the repo's core, with CONFIG; config's work and logs dirs under it."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "my root")
        os.makedirs(self.root)
        os.symlink(config.CORE, os.path.join(self.root, "core"))
        self.config = os.path.join(self.root, "orchestrator", "config.toml")
        self.write(self.config, CONFIG)
        for p in (mock.patch.object(config, "RUNS_DIR", os.path.join(self.root, "work")),
                  mock.patch.object(config, "LOGS_DIR", os.path.join(self.root, "logs")), mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("TUI_ATTACH_PREFIX", None)
        self.rd = os.path.join(self.root, "work", ID)
        self.runs = os.path.join(self.root, "logs", "runs.log")
        self.projects = os.path.join(self.tmp, "projects")
        self.gql = Gql(node(comments=[USER_NOTE]))
        self.run = Run(ENG_RUN)
        self.sh_calls, self.sh_error, self.missing, self.keychain_calls = [], None, set(), []
        self.tmux_sessions = []

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def sh(self, argv, **kw):
        self.sh_calls.append((argv, kw))
        if self.sh_error:
            raise self.sh_error
        if argv == LIST:
            return subprocess.CompletedProcess(argv, 0, "".join(f"{n}\n" for n in self.tmux_sessions), "")
        return subprocess.CompletedProcess(argv, 0)

    def keychain(self, service):
        self.keychain_calls.append(service)
        return service not in self.missing

    def plog_path(self, task="build"):
        return os.path.join(self.root, "logs", "projects", f"{task}.log")

    def plog(self, task="build"):
        return [TS.sub("<ts> ", line) for line in self.read(self.plog_path(task)).splitlines()]

    def transcript(self):
        self.write(config.transcript(ID, SID, self.projects), "")
