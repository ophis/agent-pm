"""The orchestrator's integration flows: the test's HOME with both local files ([task_labels] included), FakeLinear
over HTTP (the AGENT_PM_LINEAR seam) with the harness, role and human accounts and the task label group, fake_claude
as `claude`, run, promote and router as `python3 -P -m` subprocesses, and readers of Linear, the logs and a run's
files. TmuxFlow adds a private tmux server for the Driver's sessions and fake_gh as `gh` with REPO a bare repo."""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from urllib.parse import quote

from agent_pm.linear_orchestrator.linear import linear

from session.tests import live_tmux
from session.tests.integration.adapters.claude import fake_claude

from ..case import PY, Case, child_env
from ..unit import board_ids, run_fixtures
from ..unit.run_fixtures import ID, SID
from . import fake_gh, fake_linear

RUN, PROMOTE, ROUTER = (
    ("-m", f"agent_pm.linear_orchestrator.engine.{n}") for n in ("run", "promote", "router")
)
LIGHT_RESEARCH = "00000000-0000-4000-8000-000000000021"
TASK_LABELS = f'[task_labels]\nlight-research = "{LIGHT_RESEARCH}"\n'
_CFG = tomllib.loads(run_fixtures.local("/") + TASK_LABELS)
ROLES = tuple(_CFG["roles"])
ACCOUNTS = {r: _CFG["roles"][r]["account"] for r in ROLES}
KEYS = {r: _CFG["roles"][r]["key"] for r in ROLES}
[HUMAN] = _CFG["human_members"]
HARNESS, HARNESS_EMAIL = _CFG["harness_key"], "harness@agents.test"
PROJECT = {"id": run_fixtures.PROJECT, "name": "Widgets"}
REPO = _CFG["project_repos"][PROJECT["id"]]
_OUTPUTS = {r: o["output"] for r, o in tomllib.loads(run_fixtures.CORE_LOCAL)["roles"].items()}
[(DOCS, BRANCH)] = {(o["repo"], o["branch"]) for o in _OUTPUTS.values()}
RESEARCH_DIR, PRD_DIR = _OUTPUTS["researcher"]["dir"], _OUTPUTS["pm"]["dir"]
GH = {
    "repos": {REPO: {"full_name": REPO, "default_branch": "main", "permissions": {"push": True}}},
    "contents": {},
}
MANAGER = "manager"
TIMEOUT = 60
TAIL = 20
GUARD = (
    "import json, shutil, sys\nfrom agent_pm.linear_orchestrator.engine import config\n"
    "print(json.dumps({n: shutil.which(n, path=config.path()) for n in sys.argv[1:]}))"
)


def steps(*names, mode="new", end=0, found=(), tuis=()):
    """orchestrator.jsonl's lines of run.py's run ID, SID: launch, its tui lines, write-back steps, end."""
    return [
        f"run launch {ID} mode={mode} sid={SID}",
        *tuis,
        *(
            f"writeback step {ID} sid={SID} step={s}" + (" found=true" if s in found else "")
            for s in names
        ),
        f"run end {ID} sid={SID} exit={end}",
    ]


def jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def tail(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-TAIL:]
    except FileNotFoundError:
        return []


class Flow(Case):
    SHIMS = ("claude",)

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        state = os.path.join(self.root, "state")
        self.runs_dir, self.logs = os.path.join(state, "work"), os.path.join(state, "logs")
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as f:
            f.write(run_fixtures.local(state) + TASK_LABELS + "\n" + run_fixtures.CORE_LOCAL)
        self.fake = fake_linear.FakeLinear(harness=HARNESS)
        self.ids = {HARNESS_EMAIL: self.fake.user(HARNESS_EMAIL, "Harness", HARNESS)}
        for r in ROLES:
            self.ids[ACCOUNTS[r]] = self.fake.user(ACCOUNTS[r], r.title(), KEYS[r])
        self.ids[HUMAN] = self.fake.user(HUMAN, "Me")
        self.fake.label_group(board_ids.TASK_GROUP, "Tasks", {LIGHT_RESEARCH: "Light Research"})
        self.seam = fake_linear.seam(self, self.fake, fake_linear.serve(self, self.fake))
        self.bin = os.path.join(self.root, "bin")
        os.mkdir(self.bin)
        self.path = fake_claude.install(self.bin)
        self.claude_scenario, self.claude_log = (
            os.path.join(self.root, n) for n in ("claude.json", "claude.jsonl")
        )

    def base(self):
        return {"HOME": self.home, "PATH": self.path, "PYTHONUTF8": "1"}

    def env(self):
        return {
            # prune lists tmux sessions: a private dir (or base()'s server), never the developer's
            "TMUX_TMPDIR": self.root,
            **self.base(),
            **child_env({}),
            "TZ": "UTC",
            "AGENT_PM_PATH": self.bin,
            "CLAUDE_CONFIG_DIR": os.path.join(self.root, "claude-config"),
            linear.SEAM: self.seam,
            fake_claude.ENV: self.claude_scenario,
        }

    def guard(self):
        """config.path() must find each of SHIMS in self.bin first: never a real one."""
        res = self.script("-c", GUARD, *self.SHIMS)
        want = {n: os.path.join(self.bin, n) for n in self.SHIMS}
        self.assertEqual((res.returncode, res.stdout), (0, json.dumps(want) + "\n"), res.stderr)

    def scene(self, steps, **scenario):
        with open(self.claude_scenario, "w") as f:
            json.dump({"steps": steps, **scenario, "log": self.claude_log}, f)

    def script(self, *argv):
        return subprocess.run(
            [*PY, *argv],
            cwd=self.root,
            env=self.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def promote(self, *argv):
        return self.script(*PROMOTE, *argv)

    def router(self, *argv):
        return self.script(*ROUTER, *argv)

    def logged(self):
        return run_fixtures.logged(self.logs)

    def said(self):
        return [f"{e['src']} {run_fixtures.show(e)}" for e in self.logged()]

    def runs(self):
        return [
            {k: v for k, v in e.items() if k != "ts"}
            for e in jsonl(os.path.join(self.logs, "runs.jsonl"))
        ]

    def calls(self):
        return jsonl(self.claude_log)

    def records(self, ident):
        return jsonl(os.path.join(self.runs_dir, ident, "run.jsonl"))

    def ledger(self, ident):
        try:
            with open(os.path.join(self.runs_dir, ident, "writeback.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def requests(self):
        with self.fake.lock:
            return list(self.fake.requests)

    def sent(self, op, account=None):
        return [r.variables for r in self.requests() if r.op == op and account in (None, r.account)]

    def problems(self):
        return [(r.op, r.account, r.result) for r in self.requests() if r.result != "ok"]

    def email(self, uid):
        return uid and self.fake.users[uid]["email"]

    def issue(self, ident):
        with self.fake.lock:
            return json.loads(json.dumps(self.fake.find(ident)))

    def attachments(self, ident):
        return self.issue(ident)["attachments"]

    def subscribers(self, ident):
        return self.issue(ident)["subscribers"]

    def comments(self, ident):
        return [(self.email(c["user"]), c["body"]) for c in self.issue(ident)["comments"]]

    def history(self, ident):
        return [
            (
                fake_linear.NAMES[h["fromStateId"]],
                fake_linear.NAMES[h["toStateId"]],
                self.email(h["actorId"]),
            )
            for h in self.issue(ident)["history"]
        ]

    def state(self, ident):
        return fake_linear.NAMES[self.issue(ident)["state"]]


class TmuxFlow(Flow):
    """A private tmux server (live_tmux) with session MANAGER for the Driver's sessions; fake_gh as `gh` over self.gh
    (write_gh), REPO a bare repo that HOME's .gitconfig puts in place of github.com."""

    SHIMS = ("claude", "gh")

    def setUp(self):
        super().setUp()
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.bin = self.server.bin
        fake_gh.install(self.bin)
        repos = os.path.join(self.root, "repos")
        fake_gh.bare(repos, *REPO.split("/"))
        fake_gh.gitconfig(self.home, repos)
        self.gh_scenario, self.gh = os.path.join(self.root, "gh.json"), copy.deepcopy(GH)
        self.write_gh()
        self.guard()

    def base(self):
        return self.server.env()

    def env(self):
        return {
            **super().env(),
            fake_gh.ENV: self.gh_scenario,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
        }

    def write_gh(self):
        # atomically: the fake reads it on each call
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.gh, f)
        os.replace(tmp, self.gh_scenario)

    def publish(self, path, text):
        self.gh["contents"].setdefault(DOCS, {}).setdefault(BRANCH, {})[path] = text
        self.write_gh()

    @staticmethod
    def doc_url(path):
        return f"https://github.com/{DOCS}/blob/{BRANCH}/{quote(path)}"

    @staticmethod
    def driver(role, ident):
        return f"agent-pm-{role}-{ident}"

    def live(self, session):
        return self.server.tmux("has-session", "-t", f"={session}").returncode == 0

    def wait_end(self, sid, ident, ends=1, role="engineer", timeout=TIMEOUT):
        """Until orchestrator.jsonl holds `ends` run end lines of sid and the Driver session of role and ident is
        gone; else fails with the tails of orchestrator.jsonl and the run's run.jsonl."""
        driver = self.driver(role, ident)

        def ended():
            try:
                events = self.logged()
            except ValueError:  # a line half appended
                return False
            end = ("run", "end", sid)
            n = sum((e["src"], e["kind"], e.get("sid")) == end for e in events)
            return n >= ends and not self.live(driver)

        try:
            live_tmux.wait(ended, timeout, f"run.py's end #{ends} of {sid} and {driver} gone")
            return
        except AssertionError as e:
            timed_out = str(e)
        self.fail(
            "\n".join(
                [
                    timed_out,
                    "orchestrator.jsonl:",
                    *tail(os.path.join(self.logs, "orchestrator.jsonl")),
                    "run.jsonl:",
                    *tail(os.path.join(self.runs_dir, ident, "run.jsonl")),
                ]
            )
        )
