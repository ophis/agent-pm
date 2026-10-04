import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import HEADER, STATES, role  # noqa: E402
import pipeline  # noqa: E402
import inputs  # noqa: E402
import issues  # noqa: E402
import router  # noqa: E402
import run  # noqa: E402
import sessions  # noqa: E402
import writeback  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402

ID, UUID = "TASK-7", "11111111-2222-4333-8444-555555555555"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
PROJECT = "121166b1-191a-4461-bec4-42f1c2dc0ddd"
URL = f"https://linear.app/t/issue/{ID}"
ENGINEER, RESEARCHER, PM = "engineer@agents.test", "researcher@agents.test", "pm@agents.test"
KEY = "linear-api-key-engineer"
CONFIG = (HEADER + 'human_members = ["Me@X.com"]\n' + role("researcher") + role("pm") + role("engineer")
          + f'[project_repos]\n"{PROJECT}" = "ophis/agent-pm"\n'
          + '[core.roles.researcher.tasks.deep-research]\ngate = "python3 {{root}}/orchestrator/src/router.py --brake"\n')
PR = "https://github.com/Ophis/Agent-PM/pull/7"
DONE = {"status": "done", "title": "TASK-7: Session registry", "summary": "Opened the PR.", "url": PR}
NOW = "2026-10-04T10:00:00+08:00"
TS = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
USER_NOTE = {"body": "Use SQLite.", "createdAt": "2026-09-02T00:00:00.000Z",
             "user": {"email": "ME@x.com", "name": "Me", "isMe": False}}
LS_REMOTE = ("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "ls-remote", "--heads",
             "https://github.com/Ophis/Agent-PM.git", "TASK-7-*")
LISTING = ("gh", "api", "repos/ophis/private_docs/contents/Research?ref=main")
DESIGN_LISTING = ("gh", "api", "repos/ophis/private_docs/contents/Product%20Design?ref=main")
INPUT = f"""Reference: TASK-7
Title: TASK-7: Session registry
Repo: Ophis/Agent-PM
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


NAMES = {issues.Q_ISSUE: "issue", sessions.Q_FIND: "find", writeback.M_COMMENT: "comment", sessions.M_UPDATE: "update",
         writeback.Q_STATE: "read", writeback.M_STATE: "state", writeback.M_SUBSCRIBE: "subscribe",
         writeback.M_ATTACH: "attach", writeback.Q_ID: "id"}
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
            return {"issue": {"state": {"id": STATES["in_progress"]}, "attachments": {"nodes": []}}}
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


class Proc:
    def __init__(self, lines, rc):
        self.stdout, self.rc, self.killed = lines, rc, False

    def wait(self):
        return self.rc

    def kill(self):
        self.killed = True


def said(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def reported(rd, *lines):
    """Appends to the run's report channel, as core's report.py does."""
    with open(os.path.join(rd, ".report.jsonl"), "a") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)


def outcome(data):
    return {"kind": "outcome", "outcome": data}


def args(assignee=ENGINEER, task="engineering", mode="new"):
    return ["--issue", ID, "--project", PROJECT, "--assignee", assignee, "--sid", SID, "--task", task, "--mode", mode]


def forwarded(assignee=ENGINEER, task="engineering", mode="new"):
    return [f"--issue={ID}", f"--project={PROJECT}", f"--assignee={assignee}", f"--sid={SID}", f"--task={task}", f"--mode={mode}"]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "my root")
        os.makedirs(self.root)
        os.symlink(pipeline.CORE, os.path.join(self.root, "core"))
        self.write(os.path.join(self.root, "orchestrator", "config.toml"), CONFIG)
        for p in (mock.patch.object(pipeline, "WORK", os.path.join(self.root, "work")), mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        self.rd = os.path.join(self.root, "work", ID)
        self.runs = os.path.join(self.root, "logs", "runs.log")
        self.projects = os.path.join(self.tmp, "projects")
        self.gql = Gql(node(comments=[USER_NOTE]))
        self.run = Run(ENG_RUN)
        self.sh_calls, self.sh_error, self.missing = [], None, set()
        self.popen_calls, self.proc = [], None
        self.lines, self.rc, self.claude_stderr = [], 0, b""

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
        return subprocess.CompletedProcess(argv, 0)

    def popen(self, argv, **kw):
        self.popen_calls.append((argv, kw))
        if self.claude_stderr:
            os.write(kw["stderr"].fileno(), self.claude_stderr)
        self.proc = Proc(self.lines, self.rc)
        return self.proc

    def main(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = run.main(argv, sh=self.sh, gql=self.gql, run=self.run, popen=self.popen, runs=self.runs,
                          projects=self.projects, keychain=lambda s: s not in self.missing, root=self.root)
        self.err = err.getvalue()
        return rc

    def plog_path(self, task="engineering"):
        return os.path.join(self.root, "logs", "projects", f"{task}.log")

    def plog(self, task="engineering"):
        return [TS.sub("<ts> ", line) for line in self.read(self.plog_path(task)).splitlines()]

    def transcript(self):
        self.write(pipeline.transcript(ID, SID, self.projects), "")


class Outer(Base):
    def test_engineering_starts_the_inner_in_tmux(self):
        self.assertEqual(self.main(args()), 0)
        self.assertEqual(self.sh_calls, [(["tmux", "new-session", "-d", "-s", "agent-pm-engineer", "-c", self.rd,
                                           sys.executable, run.RUN, "--inner", "--uuid", UUID, "--target", "Ophis/Agent-PM",
                                           *forwarded()], {"check": True})])
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")), INPUT)
        self.assertEqual(os.listdir(self.rd), ["input.md"])
        self.assertEqual(self.gql.calls, [("issue", None, {"i": ID})])
        self.assertEqual(self.run.calls, [(("gh", "api", "repos/ophis/agent-pm"), 60), (LS_REMOTE, 60)])
        self.assertEqual(os.environ["PATH"], pipeline.PATH)
        self.assertEqual((self.err, os.path.exists(self.plog_path())), ("", False))

    def test_research_has_no_target(self):
        self.gql.issue = node(title="Compare queues", description="Which queue fits?")
        self.run.table = [(LISTING, res("[]"))]
        self.assertEqual(self.main(args(RESEARCHER, "deep-research")), 0)
        (argv, _), = self.sh_calls
        self.assertEqual(argv[-8:], ["--uuid", UUID, *forwarded(RESEARCHER, "deep-research")])
        self.assertNotIn("--target", argv)
        self.assertEqual(self.run.calls, [(LISTING, 60)])
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")),
                         f"Reference: TASK-7\nRepo: ophis/agent-pm\n\n{inputs.PRECEDENCE['research']}\n\n"
                         "## Question\n\nCompare queues\n\nWhich queue fits?")

    def test_design_gets_the_research_target(self):
        self.gql.issue = node(title="Queues PRD", description="Build a PRD for X.")
        self.run.table = [(DESIGN_LISTING, res("[]"))]
        self.assertEqual(self.main(args(PM, "product-design")), 0)
        (argv, _), = self.sh_calls
        self.assertNotIn("--target", argv)
        self.assertEqual(self.run.calls, [(DESIGN_LISTING, 60)])
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")),
                         f"Reference: TASK-7\nRepo: ophis/agent-pm\n\n{inputs.PRECEDENCE['design']}\n\n"
                         "## Brief\n\nQueues PRD\n\nBuild a PRD for X.")

    def test_resume_rebuilds_the_input(self):
        self.transcript()
        self.write(os.path.join(self.rd, "input.md"), "stale")
        self.assertEqual(self.main(args(mode="resume")), 0)
        self.assertEqual(self.sh_calls[0][0][-6:], forwarded(mode="resume"))
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")), INPUT)

    def test_argparse_error(self):
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            run.main(args()[:-2])
        self.assertEqual(cm.exception.code, 2)

    def test_k_and_url_are_rejected(self):
        for extra in (["--k", "1"], ["--url", URL]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                run.main(args() + extra)
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.sh_calls, [])

    def test_bad_issue_or_session_id(self):
        for bad in (["--issue", "task-7"], ["--sid", "not-a-sid"]):
            with self.subTest(bad=bad):
                argv = args()
                argv[argv.index(bad[0]) + 1] = bad[1]
                self.assertEqual(self.main(argv), 2)
                issue, sid = ("task-7", SID) if bad[0] == "--issue" else (ID, "not-a-sid")
                self.assertEqual(self.err, f"run.py: bad issue or session id: {issue} {sid}\n")
        self.assertEqual(self.sh_calls, [])

    def test_config_error_exits_1(self):
        self.write(os.path.join(self.root, "orchestrator", "config.toml"), CONFIG.replace("human_members", "bogus = 1\nhuman_members"))
        self.assertEqual(self.main(args()), 1)
        self.assertEqual(self.err, "run.py: orchestrator/config.toml: unknown keys: bogus\n")

    def test_not_a_role_account(self):
        self.assertEqual(self.main(args(assignee="x@y.com")), 2)
        self.assertEqual(self.err, "run.py: 'x@y.com' is not a role account\n")

    def test_task_not_the_roles_goes_to_the_default_task_log(self):
        self.assertEqual(self.main(args(RESEARCHER, "engineering")), 2)
        self.assertEqual(self.plog("deep-research"), ["<ts> config-error TASK-7: task 'engineering' is not one of "
                                                      "researcher's tasks (deep-research, light-research)"])
        self.assertFalse(os.path.exists(self.plog_path()))

    def test_resume_without_transcript(self):
        self.assertEqual(self.main(args(mode="resume")), 3)
        path = pipeline.transcript(ID, SID, self.projects)
        self.assertEqual(self.plog(), [f"<ts> transient TASK-7: no transcript to resume at {path}"])
        self.assertEqual(TS.sub("<ts> ", self.err), f"<ts> transient TASK-7: no transcript to resume at {path}\n")

    def test_no_keychain_item(self):
        self.missing.add(KEY)
        self.assertEqual(self.main(args()), 2)
        self.assertEqual(self.plog(), ["<ts> config-error TASK-7: no Keychain item for role key linear-api-key-engineer"])
        self.assertEqual(self.gql.calls, [])

    def test_linear_errors_are_transient(self):
        cases = [(Gql(None), "LookupError: TASK-7: issue not found"),
                 (Gql(node(), lambda n, v: SystemExit("linear api error: boom")), "SystemExit: linear api error: boom")]
        for gql, reason in cases:
            with self.subTest(reason=reason):
                self.gql = gql
                self.assertEqual(self.main(args()), 3)
                self.assertEqual(self.plog()[-1], f"<ts> transient TASK-7: Linear: {reason}")
        self.assertEqual(self.sh_calls, [])

    def test_invalid_new_bounces_without_a_run(self):
        self.run.table = [(("gh", "api"), res(code=1, stderr="gh: Not Found (HTTP 404)"))]
        reason = "project mapping ophis/agent-pm: not found or no access (HTTP 404)"
        self.assertEqual(self.main(args()), 0)
        self.assertEqual(self.gql.calls, [
            ("issue", None, {"i": ID}),
            ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
            ("comment", KEY, {"i": UUID, "b": f"Question: repo check failed: {reason}. Fix the description's `Repo:` "
                                              "line, then move this issue back to Todo."}),
            ("state", KEY, {"i": UUID, "s": STATES["in_review"]})])
        self.assertEqual(self.plog(), [f"<ts> bounce TASK-7: {reason}"])
        self.assertEqual((self.sh_calls, os.path.exists(self.rd)), ([], False))

    def test_bounce_failure_is_transient(self):
        self.run.table = [(("gh", "api"), res(code=1, stderr="HTTP 404"))]
        self.gql = Gql(node(), lambda n, v: SystemExit("linear api error: boom") if n == "comment" else None)
        self.assertEqual(self.main(args()), 3)
        self.assertEqual(self.plog(), ["<ts> transient TASK-7: bounce: SystemExit: linear api error: boom"])
        self.assertEqual(self.sh_calls, [])

    def test_invalid_resume_and_transient_check_start_nothing(self):
        self.transcript()
        cases = [("resume", res(code=1, stderr="HTTP 404"),
                  "project mapping ophis/agent-pm: not found or no access (HTTP 404)"),
                 ("new", res(code=1, stderr="HTTP 502: Bad Gateway"), "gh api repos/ophis/agent-pm: HTTP 502: Bad Gateway"),
                 ("new", subprocess.TimeoutExpired(["gh", "api"], 60),
                  "repo check: TimeoutExpired: Command '['gh', 'api']' timed out after 60 seconds")]
        for mode, r, reason in cases:
            with self.subTest(reason=reason):
                self.run.table = [(("gh", "api"), r)]
                self.assertEqual(self.main(args(mode=mode)), 3)
                self.assertEqual(self.plog()[-1], f"<ts> transient TASK-7: {reason}")
        self.assertEqual(self.gql.calls, [("issue", None, {"i": ID})] * 3)
        self.assertEqual(self.sh_calls, [])

    def test_docs_failure_is_transient(self):
        self.gql.issue = node(title="Compare queues", description="Which queue fits?")
        for r, reason in ((res(code=1, stderr="HTTP 500"), "gh api contents Research: HTTP 500"),
                          (OSError("gh missing"), "gh missing")):
            with self.subTest(reason=reason):
                self.run.table = [(LISTING, r)]
                self.assertEqual(self.main(args(RESEARCHER, "deep-research")), 3)
                self.assertEqual(self.plog("deep-research")[-1], f"<ts> transient TASK-7: docs: {reason}")
        self.assertEqual(self.sh_calls, [])

    def test_input_write_failure_leaves_no_temp(self):
        with mock.patch.object(pipeline.os, "replace", side_effect=OSError("disk full")):
            self.assertEqual(self.main(args()), 1)
        self.assertEqual((os.listdir(self.rd), self.sh_calls), ([], []))
        self.assertEqual(self.err, "run.py: input.md: OSError: disk full\n")

    def test_tmux_failure_exits_1(self):
        self.sh_error = subprocess.CalledProcessError(1, ["tmux"])
        self.assertEqual(self.main(args()), 1)
        self.assertEqual(len(self.sh_calls), 1)
        self.assertIn("run.py: tmux: CalledProcessError", self.err)


class Inner(Base):
    def setUp(self):
        super().setUp()
        self.write(os.path.join(self.rd, "input.md"), "Do it.\n")
        self.handlers = {}
        for p in (mock.patch.object(signal, "signal", lambda s, h: self.handlers.__setitem__(s, h)),
                  mock.patch.object(sessions, "now", return_value=NOW)):
            p.start()
            self.addCleanup(p.stop)

    def inner(self, assignee=ENGINEER, task="engineering", mode="new", target="Ophis/Agent-PM", uuid=UUID):
        return self.main(["--inner", "--uuid", uuid, *(["--target", target] if target else []),
                          *forwarded(assignee, task, mode)])

    def rec(self, key=KEY):
        return sessions.base(sid=SID, cwd=self.rd, key=key, started_at=NOW)

    def harness(self, rc):
        """The session start and end posts: harness account, bounded by sessions.LIMIT."""
        self.assertEqual({t for (_, service, _), t in zip(self.gql.calls, self.gql.timeouts) if service is None},
                         {sessions.LIMIT})
        find = ("find", None, {"i": ID, "p": f"Run {SID} · "})
        return [find, ("comment", None, {"i": ID, "b": sessions.body(self.rec())}),
                find, ("comment", None, {"i": ID, "b": sessions.body(self.rec(), rc, NOW)})]

    def end_line(self):
        (line,) = [x for x in self.read(self.plog_path()).splitlines() if " end " in x]
        return line

    def test_engineering_run(self):
        def lines():
            reported(self.rd, {"kind": "progress", "name": "start", "text": "first build, phase 1"})
            yield said("Working on it")
            reported(self.rd, outcome(DONE))
        self.lines = lines()
        self.claude_stderr = b"claude: warning\n"
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.plog(), [
            "<ts> launch TASK-7 mode=new session=" + SID,
            "claude: warning",
            "Progress (start): first build, phase 1",
            "<ts> writeback TASK-7: start",
            "Working on it",
            "<ts> end TASK-7 session=" + SID + " exit=0",
            "<ts> writeback TASK-7: comment",
            f"<ts> writeback TASK-7: attach:{PR}",
            "<ts> writeback TASK-7: move:in_review"])
        self.assertEqual(self.read(self.runs), self.end_line() + "\n")
        posts = self.harness(0)
        self.assertEqual(self.gql.calls, [
            *posts[:2],
            ("comment", KEY, {"i": UUID, "b": "Build started: first build, phase 1"}),
            ("read", KEY, {"i": UUID}),
            ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
            ("comment", KEY, {"i": UUID, "b": f"Build ready: Opened the PR.\n\n{PR}"}),
            ("attach", KEY, {"i": UUID, "u": PR, "t": "TASK-7: Session registry"}),
            ("read", KEY, {"i": UUID}),
            ("state", KEY, {"i": UUID, "s": STATES["in_review"]}),
            *posts[2:]])
        (argv, kw), = self.popen_calls
        self.assertEqual((argv[:2], argv[3:5], kw["cwd"]), (["claude", "-p"], ["--session-id", SID], self.rd))
        self.assertTrue(argv[2].endswith(f"Input: {self.rd}/input.md\nWorkdir: {self.rd}\n"), argv[2][-200:])
        self.assertEqual((kw["stderr"].name, kw["stderr"].mode, kw["stderr"].closed), (self.plog_path(), "a", True))
        self.assertIn("Working on it", self.err)
        self.assertEqual(os.environ["PATH"], pipeline.PATH)

    def test_no_outcome_leaves_the_issue(self):
        self.lines = [said("Working on it")]
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.plog()[-2:], ["<ts> end TASK-7 session=" + SID + " exit=0",
                                            "<ts> no-outcome TASK-7: the run returned no outcome"])
        self.assertEqual(self.gql.calls, self.harness(0))

    def test_nonzero_exit(self):
        def lines():
            reported(self.rd, outcome(DONE))
            yield from ()
        self.lines, self.rc = lines(), 1
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.plog()[-2:], ["<ts> end TASK-7 session=" + SID + " exit=1",
                                            "<ts> no-outcome TASK-7: the client exited 1"])
        self.assertEqual(self.read(self.runs), self.end_line() + "\n")
        self.assertEqual(self.gql.calls, self.harness(1))

    def test_sighup_kills_claude(self):
        def lines():
            yield said("Working on it")
            self.handlers[signal.SIGHUP](signal.SIGHUP, None)
            yield said("never")
        self.lines = lines()
        self.assertEqual(self.inner(), 0)
        self.assertTrue(self.proc.killed)
        self.assertEqual(self.plog(), ["<ts> launch TASK-7 mode=new session=" + SID, "Working on it",
                                       "<ts> end TASK-7 session=" + SID + " exit=129",
                                       "<ts> no-outcome TASK-7: no result"])
        self.assertEqual(self.gql.calls, self.harness(129))
        self.assertIn("interrupted", self.gql.calls[-1][2]["b"])
        self.assertEqual(set(self.handlers), {signal.SIGTERM, signal.SIGHUP})
        with self.assertRaises(SystemExit) as cm:
            self.handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.assertEqual(cm.exception.code, 143)

    def test_deep_research_argv_has_the_brake(self):
        self.assertEqual(self.inner(RESEARCHER, "deep-research", target=None), 0)
        (argv, _), = self.popen_calls
        self.assertIn(f"Bash(python3 {shlex.quote(self.root)}/orchestrator/src/router.py --brake)", argv)
        self.assertEqual(self.plog("deep-research")[0], "<ts> launch TASK-7 mode=new session=" + SID)

    def test_resume(self):
        self.write(os.path.join(self.rd, "progress.jsonl"), '{"name": "start"}\n')
        self.write(os.path.join(self.rd, "outcome.json"), "{}")
        self.assertEqual(self.inner(mode="resume"), 0)
        (argv, _), = self.popen_calls
        self.assertEqual((argv[3:5], argv[2].startswith(compose.RESUME)), (["--resume", SID], True))
        self.assertEqual(self.read(os.path.join(self.rd, "progress.jsonl")), '{"name": "start"}\n')
        self.assertFalse(os.path.exists(os.path.join(self.rd, "outcome.json")))
        self.assertEqual(self.plog()[0], "<ts> launch TASK-7 mode=resume session=" + SID)

    def test_run_error(self):
        def popen(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "claude")
        self.popen = popen
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.plog()[1:], [
            "<ts> run-error TASK-7: FileNotFoundError: [Errno 2] No such file or directory: 'claude'",
            "<ts> end TASK-7 session=" + SID + " exit=1", "<ts> no-outcome TASK-7: no result"])
        self.assertEqual(self.gql.calls, self.harness(1))

    def test_step_2_errors_write_the_end_line(self):
        cases = [(dict(assignee="x@y.com"), "run.py: 'x@y.com' is not a role account\n", None),
                 (dict(assignee=RESEARCHER, task="engineering"), "run.py: task 'engineering' is not one of researcher's "
                  "tasks (deep-research, light-research)\n", "deep-research")]
        for kw, err, task in cases:
            with self.subTest(err=err):
                os.makedirs(os.path.dirname(self.runs), exist_ok=True)
                open(self.runs, "w").close()
                self.assertEqual(self.inner(**kw), 1)
                self.assertEqual(self.err, err)
                self.assertEqual([TS.sub("<ts> ", x) for x in self.read(self.runs).splitlines()],
                                 ["<ts> end TASK-7 session=" + SID + " exit=1"])
                if task:
                    self.assertEqual(self.plog(task), ["<ts> end TASK-7 session=" + SID + " exit=1"])
        self.assertEqual((self.gql.calls, self.popen_calls), ([], []))

    def test_config_error_writes_the_end_line(self):
        os.remove(os.path.join(self.root, "orchestrator", "config.toml"))
        self.write(os.path.join(self.root, "orchestrator", "config.toml"), "team = 1\n")
        self.assertEqual(self.inner(), 1)
        self.assertTrue(self.err.startswith("run.py: orchestrator/config.toml: team must be"), self.err)
        self.assertEqual([TS.sub("<ts> ", x) for x in self.read(self.runs).splitlines()],
                         ["<ts> end TASK-7 session=" + SID + " exit=1"])

    def test_bad_uuid_or_target(self):
        for kw in (dict(uuid="nope"), dict(target="a/b/c")):
            with self.subTest(kw=kw):
                self.assertEqual(self.inner(**kw), 2)
                self.assertTrue(self.err.startswith("run.py: bad issue uuid or target: "), self.err)
        self.assertEqual((self.gql.calls, self.popen_calls, os.path.exists(self.runs)), ([], [], False))


class Helpers(unittest.TestCase):
    def test_log_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.log")
            sink = run.log_file(path)
            sink(drive.Event("text", "hi\x1b[2Jthere\tok"))
            sink(drive.Event("progress", "phase 1", name="start"))
            sink(drive.Event("outcome", outcome={"status": "done"}))
            with open(path) as f:
                self.assertEqual(f.read(), "hi[2Jthere\tok\nProgress (start): phase 1\n")
            run.log_file(os.path.join(d, "missing", "p.log"))(drive.Event("text", "x"))

    def test_has_key_never_reads_the_secret(self):
        for code, want in ((0, True), (44, False)):
            with mock.patch.object(run.subprocess, "run", return_value=subprocess.CompletedProcess([], code)) as m:
                self.assertIs(run.has_key("svc"), want)
            self.assertEqual(m.call_args.args[0], ["security", "find-generic-password", "-s", "svc"])

    def test_router_launches_this_file(self):
        self.assertEqual(run.RUN, router.RUN)


if __name__ == "__main__":
    unittest.main()
