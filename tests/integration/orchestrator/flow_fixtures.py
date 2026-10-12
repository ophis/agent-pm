import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from urllib.parse import quote

import config
import linear
from clients import claude as claude_client

from tests import hermetic  # first: config reads HOME at import
from tests.integration.core import fake_claude, live_tmux
from tests.integration.orchestrator import fake_gh, fake_linear
from tests.unit.orchestrator import board_ids, run_fixtures

SRC = os.path.join(config.ROOT, "orchestrator", "src")
ROUTER, PROMOTE = os.path.join(SRC, "router.py"), os.path.join(SRC, "promote.py")
ROLES = ("researcher", "pm", "engineer")
HUMAN = "me@humans.test"
PROJECT = {"id": "121166b1-191a-4461-bec4-42f1c2dc0ddd", "name": "Widgets"}
REPO = "acme/widgets"
LIGHT_RESEARCH = "00000000-0000-4000-8000-000000000021"
LOCAL = (
    board_ids.HEADER
    + f'human_members = ["{HUMAN}"]\n'
    + "".join(map(board_ids.role, ROLES))
    + f'[project_repos]\n"{PROJECT["id"]}" = "{REPO}"\n[task_labels]\nlight-research = "{LIGHT_RESEARCH}"\n'
)
_CFG = tomllib.loads(LOCAL)
with open(hermetic.FIXTURE, "rb") as _f:
    _OUTPUTS = {
        r: o["output"] for r, o in tomllib.load(_f)["roles"].items() if r in ("researcher", "pm")
    }
[(DOCS, BRANCH)] = {(o["repo"], o["branch"]) for o in _OUTPUTS.values()}
RESEARCH_DIR, PRD_DIR = _OUTPUTS["researcher"]["dir"], _OUTPUTS["pm"]["dir"]
HARNESS, HARNESS_EMAIL = _CFG["harness_key"], "harness@agents.test"
ACCOUNTS = {r: _CFG["roles"][r]["account"] for r in ROLES}
KEYS = {r: _CFG["roles"][r]["key"] for r in ROLES}
ENGINEER = ACCOUNTS["engineer"]
GH = {
    "repos": {REPO: {"full_name": REPO, "default_branch": "main", "permissions": {"push": True}}},
    "contents": {},
}
MANAGER = "manager"
SHIMS = ("claude", "gh", "python3")
TIMEOUT = 60
TAIL = 20
GUARD = (
    "import json, shutil, sys; sys.path.insert(0, sys.argv[1]); import config; "
    "print(json.dumps({n: shutil.which(n, path=config.PATH) for n in sys.argv[2:]}))"
)


def _jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _tail(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-TAIL:]
    except FileNotFoundError:
        return []


class Flow(unittest.TestCase):
    def setUp(self):
        self.apm = hermetic.home(self)
        self.home, self.logs = os.path.dirname(self.apm), os.path.join(self.apm, "logs")
        with open(os.path.join(self.apm, "orchestrator.local.toml"), "w") as f:
            f.write(LOCAL)
        shutil.copy(hermetic.FIXTURE, os.path.join(self.apm, "core.local.toml"))
        self.fake = fake_linear.FakeLinear(harness=HARNESS)
        self.ids = {HARNESS_EMAIL: self.fake.user(HARNESS_EMAIL, "Harness", HARNESS)}
        for r in ROLES:
            self.ids[ACCOUNTS[r]] = self.fake.user(ACCOUNTS[r], r.title(), KEYS[r])
        self.ids[HUMAN] = self.fake.user(HUMAN, "Me")
        self.fake.label_group(board_ids.TASK_GROUP, "Tasks", {LIGHT_RESEARCH: "Light Research"})
        self.seam = fake_linear.seam(self, self.fake, fake_linear.serve(self, self.fake))
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        fake_gh.install(self.server.bin)
        repos = os.path.join(self.server.root, "repos")
        fake_gh.bare(repos, *REPO.split("/"))
        fake_gh.gitconfig(self.home, repos)
        self.claude_scenario, self.claude_log, self.gh_scenario = (
            os.path.join(self.server.root, n) for n in ("claude.json", "claude.jsonl", "gh.json")
        )
        self.gh = copy.deepcopy(GH)
        self.write_gh()
        self.guard()

    def write_gh(self):
        # atomically: the fake reads it on each call
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.gh_scenario), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.gh, f)
        os.replace(tmp, self.gh_scenario)

    def publish(self, path, text):
        self.gh["contents"].setdefault(DOCS, {}).setdefault(BRANCH, {})[path] = text
        self.write_gh()

    @staticmethod
    def doc_url(path):
        return f"https://github.com/{DOCS}/blob/{BRANCH}/{quote(path)}"

    def guard(self):
        res = subprocess.run(
            [sys.executable, "-c", GUARD, SRC, *SHIMS],
            cwd=self.home,
            env=self.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        want = {n: os.path.join(self.server.bin, n) for n in SHIMS}
        try:
            found = json.loads(res.stdout) if res.returncode == 0 else None
        except ValueError:
            found = None
        if found != want:
            self.fail(
                f"guard: config.PATH must find {want}; exit {res.returncode}, stdout {res.stdout.strip()!r}, "
                f"stderr {res.stderr.strip()!r}"
            )

    def env(self):
        return self.server.env(
            **{
                "AGENT_PM_PATH": self.server.bin,
                linear.SEAM: self.seam,
                fake_claude.ENV: self.claude_scenario,
                fake_gh.ENV: self.gh_scenario,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ALLOW_PROTOCOL": "file",
            }
        )

    def scene(self, **scenario):
        with open(self.claude_scenario, "w") as f:
            json.dump({**scenario, "log": self.claude_log}, f)

    def router(self, *argv):
        return self._run(ROUTER, argv)

    def promote(self, *argv):
        return self._run(PROMOTE, argv)

    def _run(self, script, argv):
        return subprocess.run(
            [sys.executable, script, *argv],
            cwd=self.home,
            env=self.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    @staticmethod
    def driver(role, ident):
        return f"agent-pm-{role}-{ident}"

    def live(self, session):
        return self.server.tmux("has-session", "-t", f"={session}").returncode == 0

    def wait_end(self, sid, ident, ends=1, role="engineer", timeout=TIMEOUT):
        driver = self.driver(role, ident)

        def ended():
            try:
                events = self.logged()
            except ValueError:  # a line half appended
                return False
            end = ("run", "end", sid)
            return sum(
                (e["src"], e["kind"], e.get("sid")) == end for e in events
            ) >= ends and not self.live(driver)

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
                    *_tail(os.path.join(self.logs, "orchestrator.jsonl")),
                    "run.jsonl:",
                    *_tail(os.path.join(self.workdir(ident), "run.jsonl")),
                ]
            )
        )

    def workdir(self, ident):
        return os.path.join(self.apm, "work", ident)

    def logged(self):
        return run_fixtures.logged(self.logs)

    def runs(self):
        return [
            {k: v for k, v in d.items() if k != "ts"}
            for d in _jsonl(os.path.join(self.logs, "runs.jsonl"))
        ]

    def ledger(self, ident):
        try:
            with open(os.path.join(self.workdir(ident), "writeback.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def calls(self):
        return _jsonl(self.claude_log)

    def transcript_path(self, ident, sid):
        return claude_client.transcript(
            self.workdir(ident), sid, os.path.join(self.home, ".claude", "projects")
        )

    def transcript(self, ident, sid):
        return _jsonl(self.transcript_path(ident, sid))

    def requests(self):
        with self.fake.lock:
            return list(self.fake.requests)

    def sent(self, op, account=None):
        return [r.variables for r in self.requests() if r.op == op and account in (None, r.account)]

    def problems(self):
        return [(r.op, r.account, r.result) for r in self.requests() if r.result != "ok"]

    def attachments(self, ident):
        with self.fake.lock:
            return [dict(a) for a in self.fake.find(ident)["attachments"]]

    def subscribers(self, ident):
        with self.fake.lock:
            return list(self.fake.find(ident)["subscribers"])

    def email(self, uid):
        return uid and self.fake.users[uid]["email"]

    def comments(self, ident):
        with self.fake.lock:
            return [(self.email(c["user"]), c["body"]) for c in self.fake.find(ident)["comments"]]

    def history(self, ident):
        with self.fake.lock:
            return [
                (
                    fake_linear.NAMES[h["fromStateId"]],
                    fake_linear.NAMES[h["toStateId"]],
                    self.email(h["actorId"]),
                )
                for h in self.fake.find(ident)["history"]
            ]

    def state(self, ident):
        with self.fake.lock:
            return fake_linear.NAMES[self.fake.find(ident)["state"]]
