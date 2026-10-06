import functools
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
from router_test import STRAY, FakeLinear, blocker, issue as todo_issue, label  # noqa: E402
from attended_test import Tmux  # noqa: E402
import config  # noqa: E402
import attended  # noqa: E402
import inputs  # noqa: E402
import issues  # noqa: E402
import linear  # noqa: E402
import router  # noqa: E402
import run  # noqa: E402
import sessions  # noqa: E402
import target  # noqa: E402
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
TUI_NAME = "engineer-TASK-7-0b6f2c1e"
ATTACH = ("run.py: driver: tmux attach -t '=agent-pm-engineer-TASK-7'\n"
          f"run.py: tui: tmux attach -t '={TUI_NAME}'\n")
LAYOUT, CLOSE = attended.layout, attended.close
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
    """Appends to the agent run's report channel, as core's report.py does."""
    with open(os.path.join(rd, ".report.jsonl"), "a") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)


def outcome(data):
    return {"kind": "outcome", "outcome": data}


def args(assignee=ENGINEER, task="engineering", mode="new"):
    return ["--issue", ID, "--project", PROJECT, "--assignee", assignee, "--sid", SID, "--task", task, "--mode", mode]


def forwarded(assignee=ENGINEER, task="engineering", mode="new", sid=SID):
    return [f"--issue={ID}", f"--project={PROJECT}", f"--assignee={assignee}", f"--sid={sid}", f"--task={task}", f"--mode={mode}"]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "my root")
        os.makedirs(self.root)
        os.symlink(config.CORE, os.path.join(self.root, "core"))
        self.write(os.path.join(self.root, "orchestrator", "config.toml"), CONFIG)
        for p in (mock.patch.object(config, "WORK", os.path.join(self.root, "work")), mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        self.rd = os.path.join(self.root, "work", ID)
        self.runs = os.path.join(self.root, "logs", "runs.log")
        self.projects = os.path.join(self.tmp, "projects")
        self.gql = Gql(node(comments=[USER_NOTE]))
        self.run = Run(ENG_RUN)
        self.sh_calls, self.sh_error, self.missing, self.keychain_calls = [], None, set(), []
        self.tmux_sessions = []
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
        if argv == LIST:
            return subprocess.CompletedProcess(argv, 0, "".join(f"{n}\n" for n in self.tmux_sessions), "")
        return subprocess.CompletedProcess(argv, 0)

    def popen(self, argv, **kw):
        self.popen_calls.append((argv, kw))
        if self.claude_stderr:
            os.write(kw["stderr"].fileno(), self.claude_stderr)
        self.proc = Proc(self.lines, self.rc)
        return self.proc

    def keychain(self, service):
        self.keychain_calls.append(service)
        return service not in self.missing

    def main(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = run.main(argv, sh=self.sh, gql=self.gql, run=self.run, popen=self.popen, runs=self.runs,
                          projects=self.projects, keychain=self.keychain, root=self.root)
        self.err = err.getvalue()
        return rc

    def plog_path(self, task="engineering"):
        return os.path.join(self.root, "logs", "projects", f"{task}.log")

    def plog(self, task="engineering"):
        return [TS.sub("<ts> ", line) for line in self.read(self.plog_path(task)).splitlines()]

    def transcript(self):
        self.write(config.transcript(ID, SID, self.projects), "")


class Outer(Base):
    def test_engineering_starts_the_inner_in_tmux(self):
        self.assertEqual(self.main(args()), 0)
        self.assertEqual(self.sh_calls, [(["tmux", "new-session", "-d", "-s", "agent-pm-engineer-TASK-7", "-c", self.rd,
                                           sys.executable, run.RUN, "--inner", "--uuid", UUID, "--target", "Ophis/Agent-PM",
                                           *forwarded()], {"check": True})])
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")), INPUT)
        self.assertEqual(os.listdir(self.rd), ["input.md"])
        self.assertEqual(self.gql.calls, [("issue", None, {"i": ID})])
        self.assertEqual(self.run.calls, [(("gh", "api", "repos/ophis/agent-pm"), 60), (LS_REMOTE, 60)])
        self.assertEqual(os.environ["PATH"], config.PATH)
        self.assertEqual((self.err, os.path.exists(self.plog_path())), ("", False))

    def test_runner_headless_is_the_default(self):
        self.assertEqual(self.main(args()), 0)
        self.assertEqual(self.main(args() + ["--runner", "headless"]), 0)
        self.assertEqual(self.sh_calls[1], self.sh_calls[0])
        self.assertEqual(self.err, "")

    def test_tui_options_need_the_tui_runner(self):
        inner = ["--inner", "--uuid", UUID, "--target", "Ophis/Agent-PM"]
        events = os.path.join(self.tmp, "events.log")
        for extra in (["--split", "right"], ["--beside", "dev"], ["--runner", "headless", "--split", "below"],
                      ["--events", events], ["--opener", "mine"]):
            for where in ([], inner):
                with self.subTest(extra=extra, inner=bool(where)):
                    self.assertEqual(self.main(args() + where + extra), 2)
                    self.assertEqual(self.err, "run.py: --split, --beside, --opener and --events need --runner tui\n")
        self.assertEqual((self.sh_calls, self.gql.calls, self.keychain_calls, self.popen_calls), ([], [], [], []))
        self.assertFalse(os.path.exists(events))
        self.assertFalse(os.path.exists(self.runs))

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

    def local_clone(self):
        """A real clone (no network) of the target in the temp root, configured under [local_clones]; its realpath."""
        path = os.path.realpath(os.path.join(self.tmp, "clone"))
        for argv in (["init", "-q", path], ["-C", path, "remote", "add", "origin", "https://github.com/ophis/agent-pm.git"]):
            subprocess.run(["git", *argv], check=True)
        self.write(os.path.join(self.root, "orchestrator", "config.toml"), CONFIG + f'[local_clones]\n"Ophis/Agent-PM" = "{path}"\n')
        return path

    def test_a_configured_local_clone_is_the_inputs_repo(self):
        clone = self.local_clone()
        self.assertEqual(self.main(args()), 0)
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")), INPUT.replace("Repo: Ophis/Agent-PM", f"Repo: {clone}"))
        self.assertEqual(self.sh_calls[0][0][self.sh_calls[0][0].index("--target") + 1], "Ophis/Agent-PM")
        self.assertEqual(self.run.calls, [(("gh", "api", "repos/ophis/agent-pm"), 60), (LS_REMOTE, 60)])
        self.assertEqual(self.err, "")

    def test_research_and_design_name_the_local_clone(self):
        clone = self.local_clone()
        for who, task, listing in ((RESEARCHER, "deep-research", LISTING), (PM, "product-design", DESIGN_LISTING)):
            with self.subTest(task=task):
                self.run.table = [(listing, res("[]"))]
                self.assertEqual(self.main(args(who, task)), 0)
                self.assertEqual(self.read(os.path.join(self.rd, "input.md")).splitlines()[1], f"Repo: {clone}")
                self.assertNotIn("--target", self.sh_calls[-1][0])

    def test_a_resume_over_a_clone_checkout_keeps_its_repo(self):
        self.local_clone()
        self.transcript()
        os.makedirs(os.path.join(config.WORK, ID, config.CLONES[0], "Ophis", "Agent-PM", ".git"))
        self.run.table = [(("git", *target.GUARD, "-C"), res()), *ENG_RUN]
        self.assertEqual(self.main(args(mode="resume")), 0)
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")), INPUT)
        self.assertEqual([argv[0] for argv, _ in self.run.calls], ["gh", "git", "git"])

    def test_resume_rebuilds_the_input(self):
        self.transcript()
        self.write(os.path.join(self.rd, "input.md"), "stale")
        self.assertEqual(self.main(args(mode="resume")), 0)
        self.assertEqual(self.sh_calls[0][0][-6:], forwarded(mode="resume"))
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")), INPUT)

    def test_argparse_errors(self):
        for argv in (args()[:-2], args() + ["--k", "1"], args() + ["--url", URL]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                run.main(argv)
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.sh_calls, [])

    def test_bad_issue_or_session_id(self):
        for bad in (["--issue", "task-7"], ["--sid", "not-a-sid"], ["--sid", "z" * 36]):
            with self.subTest(bad=bad):
                argv = args()
                argv[argv.index(bad[0]) + 1] = bad[1]
                self.assertEqual(self.main(argv), 2)
                issue, sid = ("task-7", SID) if bad[0] == "--issue" else (ID, bad[1])
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
        path = config.transcript(ID, SID, self.projects)
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
            ("reread", KEY, {"i": UUID}),
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
        with mock.patch.object(drive.os, "replace", side_effect=OSError("disk full")):
            self.assertEqual(self.main(args()), 1)
        self.assertEqual((os.listdir(self.rd), self.sh_calls), ([], []))
        self.assertEqual(self.err, "run.py: input.md: OSError: disk full\n")

    def test_tmux_failure_exits_1(self):
        self.sh_error = subprocess.CalledProcessError(1, ["tmux"])
        self.assertEqual(self.main(args()), 1)
        self.assertEqual(len(self.sh_calls), 1)
        self.assertIn("run.py: tmux: CalledProcessError", self.err)


class Attended(Base):
    """The outer with the tui runner; attended.layout sees a fake tmux, no $TMUX or $ITERM_SESSION_ID, and iTerm2's
    $TERM_PROGRAM."""
    def setUp(self):
        super().setUp()
        for k in ("TMUX", "ITERM_SESSION_ID"):  # Base's patch.dict restores them
            os.environ.pop(k, None)
        os.environ["TERM_PROGRAM"] = "iTerm.app"

    def tui(self, *extra, live=(), assignee=ENGINEER):
        self.tmux = Tmux(live)
        place = functools.partial(LAYOUT, proc=self.tmux)
        with mock.patch.object(attended, "layout", place):
            return self.main(args(assignee) + ["--runner", "tui", *extra])

    def driver(self, *tail, iterm=""):
        return [(["tmux", "new-session", "-d", "-e", f"ITERM_SESSION_ID={iterm}", "-s",
                  "agent-pm-engineer-TASK-7", "-c", self.rd,
                  sys.executable, run.RUN, "--inner", "--uuid", UUID, "--target", "Ophis/Agent-PM", *forwarded(), *tail],
                 {"check": True})]

    def rename_engineer(self, name):
        """The root's core (linked to the real one) and orchestrator config with the engineer role named `name`."""
        core, roles = os.path.join(self.root, "core"), os.path.join("team", "roles")
        os.remove(core)
        for parent, own in (("", {"config", "team"}), ("team", {"roles"}), (roles, set())):
            os.makedirs(os.path.join(core, parent), exist_ok=True)
            for n in set(os.listdir(os.path.join(config.CORE, parent))) - own:
                os.symlink(os.path.join(config.CORE, parent, n), os.path.join(core, parent, n))
        os.symlink(os.path.join(config.CORE, roles, "engineer.md"), os.path.join(core, roles, f"{name}.md"))
        self.write(os.path.join(core, compose.CONFIG),
                   self.read(os.path.join(config.CORE, compose.CONFIG)).replace("[roles.engineer", f"[roles.{name}"))
        self.write(os.path.join(self.root, "orchestrator", "config.toml"), CONFIG.replace(role("engineer"), role(name)))

    def test_beside_a_session(self):
        self.assertEqual(self.tui("--split", "below", "--beside", "dev", live=["dev"]), 0)
        self.assertEqual(self.sh_calls, self.driver("--runner=tui", "--split=below", "--beside=dev"))
        self.assertEqual(self.err, ATTACH)
        self.assertEqual(self.read(os.path.join(self.rd, "input.md")), INPUT)

    def test_an_iterm2_pane_gets_the_tui_and_a_detached_driver(self):
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        self.assertEqual(self.tui(), 0)
        self.assertEqual(self.sh_calls, self.driver("--runner=tui", "--opener=w0t0p0:ABC", iterm="w0t0p0:ABC"))
        self.assertEqual(self.err, ATTACH)

    def test_the_callers_tmux_session_is_the_opener_never_beside(self):
        os.environ.update(TMUX="/tmp/tmux-1/default,1,0", TMUX_PANE="%3")
        self.assertEqual(self.tui(live=["mine"]), 0)
        self.assertEqual(self.sh_calls, self.driver("--runner=tui", "--opener=mine"))
        self.assertEqual(self.err, ATTACH)

    def test_events_reach_the_inner_as_an_absolute_path(self):
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        events = os.path.join(self.tmp, "events.log")
        self.assertEqual(self.tui("--split", "below", "--events", os.path.join(self.tmp, ".", "events.log")), 0)
        self.assertEqual(self.sh_calls, self.driver("--runner=tui", "--split=below", "--opener=w0t0p0:ABC",
                                                    f"--events={events}", iterm="w0t0p0:ABC"))
        self.assertEqual(os.stat(events).st_mode & 0o777, 0o600)

    def test_a_bad_events_file_starts_nothing(self):
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        cases = [(self.tmp, f"events file {self.tmp}: Is a directory"),
                 (os.path.join(self.tmp, "a#b"), f"events file {self.tmp}/a#b: tmux would misread it")]
        for path, msg in cases:
            with self.subTest(msg=msg):
                self.assertEqual(self.tui("--events", path), 2)
                self.assertEqual(self.err, f"run.py: {msg}\n")
        self.assertEqual((self.sh_calls, self.gql.calls, self.keychain_calls, self.run.calls), ([], [], [], []))
        self.assertFalse(os.path.exists(self.rd))

    def test_a_role_outside_the_session_name_contract_starts_nothing(self):
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        self.rename_engineer("eng_x")
        self.assertEqual(self.tui(assignee="eng_x@agents.test"), 2)
        (line,) = self.plog()
        self.assertTrue(line.startswith("<ts> config-error TASK-7: TUI session prefix 'eng_x-TASK-7': want "), line)
        self.assertEqual((self.sh_calls, self.gql.calls, self.keychain_calls, self.run.calls), ([], [], [], []))
        self.assertEqual(self.main(args("eng_x@agents.test")), 0)  # headless: no TUI session to name

    def test_attach_lines_come_before_the_driver_session(self):
        printed = []
        self.sh = lambda argv, **kw: printed.append(sys.stderr.getvalue())
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        self.assertEqual(self.tui(), 0)
        self.assertEqual(printed, [ATTACH])

    def test_no_place_for_the_pane_starts_nothing(self):
        cases = [((), "no pane to show the TUI beside (no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)): "
                      "run from tmux or iTerm2, or pass --beside SESSION"),
                 (("--split", "left"), "split must be one of right, below"),
                 (("--beside", "gone"), "no tmux session gone")]
        for extra, msg in cases:
            with self.subTest(msg=msg):
                self.assertEqual(self.tui(*extra), 2)
                self.assertEqual(self.err, f"run.py: {msg}\n")
        self.assertEqual((self.sh_calls, self.gql.calls, self.keychain_calls, self.run.calls), ([], [], [], []))
        self.assertFalse(os.path.exists(self.rd))


class ClaimLinear(FakeLinear):
    """router_test's fake Linear for the claim; issues.Q_ISSUE, the outer's read, gets the outer's issue node."""
    def __init__(self, todo, issue):
        super().__init__([todo])
        self.issue = issue

    def __call__(self, query, **v):
        if query == issues.Q_ISSUE:
            self.queries.append((query, v))
            return {"issue": self.issue}
        return super().__call__(query, **v)


class AttendedEntry(Base):
    """run.py --issue ID --tui: the claim on a fake Linear, then the outer with the tui runner."""
    def setUp(self):
        super().setUp()
        self.todo = todo_issue(ID, "Todo", "engineer")
        self.todo["project"] = {"id": PROJECT, "name": "Agent PM"}
        self.gql = ClaimLinear(self.todo, node(comments=[USER_NOTE]))
        self.tmux = Tmux(live=["dev"])
        for k in ("TMUX", "ITERM_SESSION_ID"):  # Base's patch.dict restores them
            os.environ.pop(k, None)
        os.environ["TERM_PROGRAM"] = "iTerm.app"
        p = mock.patch.object(attended, "layout", functools.partial(LAYOUT, proc=self.tmux))
        p.start()
        self.addCleanup(p.stop)

    def sh(self, argv, **kw):
        """Base's, noting runs.log's text when the driver session starts."""
        if argv[1] == "new-session":
            self.at_launch.append(self.read(self.runs))
        return super().sh(argv, **kw)

    def entry(self, *extra):
        return self.main(["--issue", ID, "--tui", *extra])

    def said(self):
        return [TS.sub("", line) for line in self.err.splitlines()]

    def test_claims_then_starts_the_run_attended(self):
        events = os.path.join(self.tmp, "events.log")
        cases = [(("--split", "below", "--beside", "dev"), "", ["--runner=tui", "--split=below", "--beside=dev"]),
                 ((), "w0t0p0:ABC", ["--runner=tui", "--opener=w0t0p0:ABC"]),
                 (("--events", events), "w0t0p0:ABC", ["--runner=tui", "--opener=w0t0p0:ABC", f"--events={events}"])]
        for extra, iterm, tail in cases:
            with self.subTest(extra=extra):
                os.environ["ITERM_SESSION_ID"] = iterm
                self.todo["state"], self.sh_calls, self.gql.mutations = "Todo", [], []
                self.tmux_sessions = [TUI_NAME, "agent-pm-engineer-TASK-70", "agent-pm-engineer-TASK-8"]
                if os.path.exists(self.runs):
                    os.remove(self.runs)
                self.at_launch = []
                self.assertEqual(self.entry(*extra), 0)
                (line,) = [TS.sub("", x) for x in self.read(self.runs).splitlines()]
                sid = line.split(" ")[2].removeprefix("session=")
                self.assertRegex(sid, config.UUID_RE)
                self.assertEqual(line, router.start_line(ID, sid, "engineering", self.projects))
                self.assertEqual(self.at_launch, [self.read(self.runs)])
                self.assertEqual(self.sh_calls, [
                    (LIST, {"capture_output": True, "text": True}),
                    (["tmux", "new-session", "-d", "-e", f"ITERM_SESSION_ID={iterm}", "-s",
                      "agent-pm-engineer-TASK-7", "-c", self.rd,
                      sys.executable, run.RUN, "--inner", "--uuid", UUID, "--target", "Ophis/Agent-PM", *forwarded(sid=sid),
                      *tail], {"check": True})])
                self.assertEqual(self.said(), ["pick: TASK-7 (1 in queue)", "claim: TASK-7 task=engineering",
                                               "run.py: driver: tmux attach -t '=agent-pm-engineer-TASK-7'",
                                               f"run.py: tui: tmux attach -t '=engineer-TASK-7-{sid[:8]}'"])
                self.assertEqual(self.gql.mutations, [(linear.M_STATE, {"i": ID, "s": STATES["in_progress"]})])
                self.assertEqual(self.read(os.path.join(self.rd, "input.md")), INPUT)

    def test_only_issue_split_beside_and_events(self):
        for argv in (["--tui"], ["--tui", "--split", "below"], *(["--issue", ID, "--tui", *x] for x in (
                ["--project", PROJECT], ["--assignee", ENGINEER], ["--sid", SID], ["--task", "engineering"], ["--mode", "new"],
                ["--inner"], ["--uuid", UUID], ["--target", "Ophis/Agent-PM"], ["--runner", "tui"], ["--runner", "headless"],
                ["--opener", "mine"], ["--spl", "below"], ["--bes", "dev"], ["--eve", "x"]))):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                run.main(argv, sh=self.sh, gql=self.gql, run=self.run, popen=self.popen, runs=self.runs,
                         projects=self.projects, keychain=self.keychain, root=self.root)
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual((self.sh_calls, self.gql.queries, self.tmux.calls), ([], [], []))

    def test_bad_issue_id(self):
        self.assertEqual(self.main(["--issue", "task-7", "--tui"]), 2)
        self.assertEqual(self.err, "run.py: bad issue id: task-7\n")
        self.assertEqual((self.sh_calls, self.gql.queries, self.tmux.calls), ([], [], []))

    def test_config_error_exits_1(self):
        self.write(os.path.join(self.root, "orchestrator", "config.toml"), CONFIG.replace("human_members", "bogus = 1\nhuman_members"))
        self.assertEqual(self.entry(), 1)
        self.assertEqual(self.err, "run.py: orchestrator/config.toml: unknown keys: bogus\n")
        self.assertEqual((self.sh_calls, self.gql.queries, self.tmux.calls), ([], [], []))

    def test_no_place_for_the_pane_or_a_bad_events_file_before_any_linear_call(self):
        cases = [((), "no pane to show the TUI beside (no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)): "
                      "run from tmux or iTerm2, or pass --beside SESSION"),
                 (("--split", "left"), "split must be one of right, below"),
                 (("--beside", "gone"), "no tmux session gone"),
                 (("--beside", "dev", "--events", self.tmp), f"events file {self.tmp}: Is a directory")]
        for extra, msg in cases:
            with self.subTest(msg=msg):
                self.assertEqual(self.entry(*extra), 2)
                self.assertEqual(self.err, f"run.py: {msg}\n")
        self.assertEqual((self.sh_calls, self.gql.queries, os.path.exists(self.runs)), ([], [], False))
        self.assertEqual(os.environ["PATH"], config.PATH)

    def test_a_live_run_is_refused(self):
        for role in ("engineer", "pm"):
            with self.subTest(role=role):
                self.sh_calls, self.tmux_sessions = [], [f"agent-pm-{role}-{ID}"]
                self.assertEqual(self.entry("--beside", "dev"), 1)
                self.assertEqual(self.err, f"run.py: {ID} has a live agent run: tmux attach -t '=agent-pm-{role}-{ID}'\n")
                self.assertEqual(self.sh_calls, [(LIST, {"capture_output": True, "text": True})])
        self.assertEqual((self.gql.queries, os.path.exists(self.runs)), ([], False))

    def test_nothing_claimed_exits_1_without_a_start_line(self):
        cases = [(dict(state="In Progress"), "pick: TASK-7 is not a Todo issue assigned to a role account", "In Progress"),
                 (dict(inverseRelations={"nodes": [blocker("TASK-9")]}), "blocked: TASK-7 by TASK-9", "Todo"),
                 (dict(labels=[label("Stray", STRAY)]),
                  "claim: TASK-7 bad task label; In Review", "In Review")]
        for change, said, state in cases:
            with self.subTest(said=said):
                self.todo.update(state="Todo", inverseRelations={"nodes": []}, labels=[])
                self.todo.update(change)
                self.sh_calls = []
                self.assertEqual(self.entry("--beside", "dev"), 1)
                self.assertIn(said, self.said())
                self.assertEqual((self.todo["state"], self.sh_calls), (state, [(LIST, {"capture_output": True, "text": True})]))
        self.assertFalse(os.path.exists(self.runs))


class Inner(Base):
    def setUp(self):
        super().setUp()
        self.write(os.path.join(self.rd, "input.md"), "Do it.\n")
        self.handlers, self.tmux = {}, Tmux()
        for p in (mock.patch.object(signal, "signal", lambda s, h: self.handlers.__setitem__(s, h)),
                  mock.patch.object(sessions, "now", return_value=NOW),
                  mock.patch.object(attended, "close", lambda ident, **kw: CLOSE(ident, proc=self.tmux, **kw))):
            p.start()
            self.addCleanup(p.stop)

    def inner(self, assignee=ENGINEER, task="engineering", mode="new", target="Ophis/Agent-PM", uuid=UUID, extra=()):
        return self.main(["--inner", "--uuid", uuid, *(["--target", target] if target else []),
                          *forwarded(assignee, task, mode), *extra])

    def rec(self):
        return sessions.base(sid=SID, cwd=self.rd, started_at=NOW)

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
            ("reread", KEY, {"i": UUID}),
            ("state", KEY, {"i": UUID, "s": STATES["in_review"]}),
            *posts[2:]])
        (argv, kw), = self.popen_calls
        self.assertEqual((argv[:2], argv[3:5], kw["cwd"]), (["claude", "-p"], ["--session-id", SID], self.rd))
        self.assertTrue(argv[2].endswith(f"Input: {self.rd}/input.md\nWorkdir: {self.rd}\n"), argv[2][-200:])
        self.assertEqual((kw["stderr"].name, kw["stderr"].mode, kw["stderr"].closed), (self.plog_path(), "a", True))
        self.assertIn("Working on it", self.err)
        self.assertEqual(os.environ["PATH"], config.PATH)

    def test_no_outcome_leaves_the_issue(self):
        self.lines = [said("Working on it")]
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.plog()[-2:], ["<ts> end TASK-7 session=" + SID + " exit=0",
                                            "<ts> no-outcome TASK-7: the agent run returned no outcome"])
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
        self.assertEqual(set(self.handlers), {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.assertRaises(SystemExit) as cm:
                self.handlers[sig](sig, None)
            self.assertEqual(cm.exception.code, code)

    def test_tui_runs_as_headless_does_but_for_runner_layout_and_session_naming(self):
        seen = []
        for extra in ((), ("--runner=tui", "--split=below", "--beside=dev", "--opener=w0t0p0:ABC", "--events=/x/ev.log")):
            self.gql = Gql(node())
            with mock.patch.object(drive, "start", return_value=drive.Result(0, drive.Outcome(**DONE))) as start:
                self.assertEqual(self.inner(extra=extra), 0)
            os.remove(os.path.join(self.rd, writeback.LEDGER))
            (pos, kw), = start.call_args_list
            seen.append(([kw.pop(k) for k in ("runner", "layout", "prefix", "events")], pos,
                         [s.__qualname__ for s in kw.pop("sinks")], sorted(kw), self.gql.calls))
        (h_own, *headless), (t_own, *tui) = seen
        self.assertEqual(h_own, ["headless", None, None, None])
        self.assertEqual(t_own, ["tui", drive.Layout("below", "dev", "w0t0p0:ABC"), "engineer-TASK-7", "/x/ev.log"])
        self.assertEqual(tui, headless)
        self.assertIn(("state", KEY, {"i": UUID, "s": STATES["in_review"]}), headless[-1])
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs", "tui")))

    def test_close_runs_before_every_run(self):
        mine = ["engineer-TASK-7-aaaaaaaa", "pm-TASK-7-bbbbbbbb"]
        other = ["engineer-TASK-70-cccccccc", "agent-pm-engineer-TASK-7"]
        for extra in ((), ("--runner=tui",)):
            with self.subTest(extra=extra):
                self.tmux = Tmux(live=[*mine, *other])
                seen = []
                self.gql = Gql(node(), lambda name, v: seen.append(list(self.tmux.live)))
                with mock.patch.object(drive, "start", return_value=drive.Result(0, None)):
                    self.assertEqual(self.inner(extra=extra), 0)
                self.assertEqual(self.plog()[-5:-2], ["<ts> launch TASK-7 mode=new session=" + SID,
                                                      *(f"<ts> tui-closed TASK-7 {n}" for n in mine)])
                self.assertEqual(self.tmux.tmux_calls(), ["list-sessions", "kill-session", "kill-session"])
                self.assertEqual(seen[0], other)

    def test_close_results_become_tui_lines(self):
        closed = [attended.Closed("closed", "e-TASK-7-aaaaaaaa"),
                  attended.Closed("error", "e-TASK-7-bbbbbbbb", "TuiError: boom"),
                  attended.Closed("error", None, "TuiError: tmux: nope")]
        with mock.patch.object(attended, "close", return_value=closed), \
                mock.patch.object(drive, "start", return_value=drive.Result(0, None)):
            self.assertEqual(self.inner(), 0)
        self.assertEqual(self.plog()[1:4], ["<ts> tui-closed TASK-7 e-TASK-7-aaaaaaaa",
                                            "<ts> tui-error TASK-7 e-TASK-7-bbbbbbbb: TuiError: boom",
                                            "<ts> tui-error TASK-7: TuiError: tmux: nope"])

    def test_bad_layout_exits_2(self):
        cases = [(("--runner=tui", "--split=left"), "layout split 'left': want one of right, below"),
                 (("--runner=tui", "--beside=a:b"), "layout beside 'a:b': want [A-Za-z0-9_-]+"),
                 (("--runner=tui", "--opener=a b"), "layout opener 'a b': want a tmux session name or an iTerm2 session id"),
                 (("--runner=tui", "--opener=w0:a:b"),
                  "layout opener 'w0:a:b': want a tmux session name or an iTerm2 session id")]
        for extra, msg in cases:
            with self.subTest(msg=msg):
                self.assertEqual(self.inner(extra=extra), 2)
                self.assertEqual(self.err, f"run.py: {msg}\n")
        self.assertEqual((self.gql.calls, self.popen_calls, os.path.exists(self.runs)), ([], [], False))

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
    def test_has_key_never_reads_the_secret(self):
        for code, want in ((0, True), (44, False)):
            with mock.patch.object(run.subprocess, "run", return_value=subprocess.CompletedProcess([], code)) as m:
                self.assertIs(run.has_key("svc"), want)
            self.assertEqual(m.call_args.args[0], ["security", "find-generic-password", "-s", "svc"])

    def test_router_launches_this_file(self):
        self.assertEqual(run.RUN, router.RUN)


if __name__ == "__main__":
    unittest.main()
