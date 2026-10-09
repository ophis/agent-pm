"""Flow, the base of the orchestrator's integration flows: router.py and promote.py run as processes against the fake
Linear (fake_linear), the fake gh and a bare repo (fake_gh) and a private tmux server whose bin dir holds the fake
claude (core's live_tmux and fake_claude). The constants name what setUp seeds; a test seeds its own issues."""
import json
import os
import shutil
import subprocess
import sys
import tomllib
import unittest

import hermetic  # first: config reads HOME at import
import board_ids
import config
sys.path.append(os.path.join(config.ROOT, "core", "src", "tests", "integration"))
import clients  # noqa: E402
import fake_claude  # noqa: E402
import fake_gh  # noqa: E402
import fake_linear  # noqa: E402
import linear  # noqa: E402
import live_tmux  # noqa: E402
import run_fixtures  # noqa: E402

SRC = os.path.join(config.ROOT, "orchestrator", "src")
ROUTER, PROMOTE = os.path.join(SRC, "router.py"), os.path.join(SRC, "promote.py")
ROLES = ("researcher", "pm", "engineer")
HUMAN = "me@humans.test"
PROJECT = {"id": "121166b1-191a-4461-bec4-42f1c2dc0ddd", "name": "Widgets"}
REPO = "acme/widgets"
LOCAL = (board_ids.HEADER + f'human_members = ["{HUMAN}"]\n' + "".join(map(board_ids.role, ROLES))
         + f'[project_repos]\n"{PROJECT["id"]}" = "{REPO}"\n')
_CFG = tomllib.loads(LOCAL)
HARNESS, HARNESS_EMAIL = _CFG["harness_key"], "harness@agents.test"
ACCOUNTS = {r: _CFG["roles"][r]["account"] for r in ROLES}
KEYS = {r: _CFG["roles"][r]["key"] for r in ROLES}
ENGINEER = ACCOUNTS["engineer"]
GH = {"repos": {REPO: {"full_name": REPO, "default_branch": "main", "permissions": {"push": True}}}}
MANAGER = "manager"
SHIMS = ("claude", "gh", "python3")
TIMEOUT = 60   # seconds: a router.py or promote.py process; an agent run to its end
TAIL = 20   # lines of each record a wait_end failure shows
GUARD = ("import json, shutil, sys; sys.path.insert(0, sys.argv[1]); import config; "
         "print(json.dumps({n: shutil.which(n, path=config.PATH) for n in sys.argv[2:]}))")


def _jsonl(path):
    """A JSON-lines file's objects; none while it is missing."""
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _tail(path):
    """A file's last TAIL lines as they are; none while it is missing."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-TAIL:]
    except FileNotFoundError:
        return []


class Flow(unittest.TestCase):
    """A HOME of the test's own, LOCAL and core's fixture its local configs; the fake Linear seeded with the team, the
    task label group, the harness, role (HARNESS, KEYS) and human users, served and named by AGENT_PM_LINEAR; a private
    tmux server, session MANAGER started, its bin dir (AGENT_PM_PATH) holding the fake claude, python3 and gh; REPO a
    bare repo that ~/.gitconfig maps github.com to. Fails unless the guard holds."""

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
        self.fake.label_group(board_ids.TASK_GROUP, "Tasks", {})
        self.seam = fake_linear.seam(self, self.fake, fake_linear.serve(self, self.fake))
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        fake_gh.install(self.server.bin)
        repos = os.path.join(self.server.root, "repos")
        fake_gh.bare(repos, *REPO.split("/"))
        fake_gh.gitconfig(self.home, repos)
        self.claude_scenario, self.claude_log, self.gh_scenario = (
            os.path.join(self.server.root, n) for n in ("claude.json", "claude.jsonl", "gh.json"))
        with open(self.gh_scenario, "w") as f:
            json.dump(GH, f)
        self.guard()

    def guard(self):
        """Fails unless config.PATH, in a process with env(), finds this test's claude, gh and python3 first."""
        res = subprocess.run([sys.executable, "-c", GUARD, SRC, *SHIMS], cwd=self.home, env=self.env(),
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
        want = {n: os.path.join(self.server.bin, n) for n in SHIMS}
        try:
            found = json.loads(res.stdout) if res.returncode == 0 else None
        except ValueError:
            found = None
        if found != want:
            self.fail(f"guard: config.PATH must find {want}; exit {res.returncode}, stdout {res.stdout.strip()!r}, "
                      f"stderr {res.stderr.strip()!r}")

    def env(self):
        """The env of every process the flow starts: live_tmux.Server.env() plus the seams' and the fakes'."""
        return self.server.env(**{"AGENT_PM_PATH": self.server.bin, linear.SEAM: self.seam,
                                  fake_claude.ENV: self.claude_scenario, fake_gh.ENV: self.gh_scenario,
                                  "GIT_CONFIG_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": "file"})

    def scene(self, **scenario):
        """Writes the fake claude's scenario (fake_claude's docstring), its log this test's."""
        with open(self.claude_scenario, "w") as f:
            json.dump({**scenario, "log": self.claude_log}, f)

    def router(self, *argv):
        return self._run(ROUTER, argv)

    def promote(self, *argv):
        return self._run(PROMOTE, argv)

    def _run(self, script, argv):
        return subprocess.run([sys.executable, script, *argv], cwd=self.home, env=self.env(), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=TIMEOUT)

    @staticmethod
    def driver(role, ident):
        """The agent run's driver session."""
        return f"agent-pm-{role}-{ident}"

    def live(self, session):
        return self.server.tmux("has-session", "-t", f"={session}").returncode == 0

    def wait_end(self, sid, ident, ends=1, role="engineer", timeout=TIMEOUT):
        """Returns once orchestrator.jsonl has `ends` run.py end events for sid (a resume keeps its sid; a killed run.py
        logs none) and the driver session is gone; a timeout fails with the tails of orchestrator.jsonl and the run's
        run.jsonl."""
        driver = self.driver(role, ident)

        def ended():
            try:
                events = self.logged()
            except ValueError:   # a line half appended
                return False
            end = ("run", "end", sid)
            return sum((e["src"], e["kind"], e.get("sid")) == end for e in events) >= ends and not self.live(driver)
        try:
            live_tmux.wait(ended, timeout, f"run.py's end #{ends} of {sid} and {driver} gone")
            return
        except AssertionError as e:
            timed_out = str(e)
        self.fail("\n".join([timed_out, "orchestrator.jsonl:", *_tail(os.path.join(self.logs, "orchestrator.jsonl")),
                             "run.jsonl:", *_tail(os.path.join(self.workdir(ident), "run.jsonl"))]))

    def workdir(self, ident):
        return os.path.join(self.apm, "work", ident)

    def logged(self):
        """orchestrator.jsonl's events (run_fixtures.logged)."""
        return run_fixtures.logged(self.logs)

    def runs(self):
        """runs.jsonl's lines without ts."""
        return [{k: v for k, v in d.items() if k != "ts"} for d in _jsonl(os.path.join(self.logs, "runs.jsonl"))]

    def ledger(self, ident):
        """The issue's writeback.json, {sid: [step…]}; {} while it is missing."""
        try:
            with open(os.path.join(self.workdir(ident), "writeback.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def calls(self):
        """The fake claude's invocations from its log, {pid, argv, cwd} each."""
        return _jsonl(self.claude_log)

    def transcript(self, ident, sid):
        """The lines of sid's transcript, the fake claude's in the issue's workdir."""
        projects = os.path.join(self.home, ".claude", "projects")
        return _jsonl(clients.claude.transcript(self.workdir(ident), sid, projects))

    def requests(self):
        """The fake Linear's request log (fake_linear.Request)."""
        with self.fake.lock:
            return list(self.fake.requests)

    def sent(self, op, account=None):
        """The variables of each `op` request (by `account`, an email, when given), oldest first."""
        return [r.variables for r in self.requests() if r.op == op and account in (None, r.account)]

    def problems(self):
        """(op, account, result) of each request not answered ok."""
        return [(r.op, r.account, r.result) for r in self.requests() if r.result != "ok"]

    def attachments(self, ident):
        """The issue's attachments, {url, title} each, oldest first."""
        with self.fake.lock:
            return [dict(a) for a in self.fake.find(ident)["attachments"]]

    def subscribers(self, ident):
        """The issue's subscribers' emails, in subscribe order."""
        with self.fake.lock:
            return list(self.fake.find(ident)["subscribers"])

    def email(self, uid):
        return uid and self.fake.users[uid]["email"]

    def comments(self, ident):
        """(author email, None for an integration's; body) of each comment on the issue, oldest first."""
        with self.fake.lock:
            return [(self.email(c["user"]), c["body"]) for c in self.fake.find(ident)["comments"]]

    def history(self, ident):
        """(from, to, actor email) of each move of the issue, oldest first; states as board_ids.STATES keys."""
        with self.fake.lock:
            return [(fake_linear.NAMES[h["fromStateId"]], fake_linear.NAMES[h["toStateId"]], self.email(h["actorId"]))
                    for h in self.fake.find(ident)["history"]]

    def state(self, ident):
        """The issue's state as a board_ids.STATES key."""
        with self.fake.lock:
            return fake_linear.NAMES[self.fake.find(ident)["state"]]
