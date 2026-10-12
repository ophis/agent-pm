"""The orchestrator's integration flows: the test's HOME with both local files, FakeLinear over HTTP (the
AGENT_PM_LINEAR seam) with the harness, role and human accounts, fake_claude as `claude`, run and promote as
`python3 -P -m` subprocesses, and readers of Linear, the logs and a run's files."""

import json
import os
import shutil
import subprocess
import tempfile
import tomllib

from agent_pm_orchestrator import linear

from new.tests import fake_claude
from new.tests.case import ORCH_ROOT, PY, Case, child_env
from new.tests.integration.orchestrator import fake_linear
from new.tests.unit.orchestrator import run_fixtures
from new.tests.unit.orchestrator.run_fixtures import ID, SID

RUN, PROMOTE = (("-m", f"agent_pm_orchestrator.{n}") for n in ("run", "promote"))
_CFG = tomllib.loads(run_fixtures.local("/"))
ROLES = tuple(_CFG["roles"])
ACCOUNTS = {r: _CFG["roles"][r]["account"] for r in ROLES}
KEYS = {r: _CFG["roles"][r]["key"] for r in ROLES}
[HUMAN] = _CFG["human_members"]
HARNESS, HARNESS_EMAIL = _CFG["harness_key"], "harness@agents.test"
PROJECT = {"id": run_fixtures.PROJECT, "name": "Widgets"}
TIMEOUT = 60
GUARD = (
    "import shutil\nfrom agent_pm_orchestrator import config\n"
    "print(shutil.which('claude', path=config.path()))"
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


class Flow(Case):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        state = os.path.join(self.root, "state")
        self.runs_dir, self.logs = os.path.join(state, "work"), os.path.join(state, "logs")
        for name, text in (
            ("orchestrator-new.local.toml", run_fixtures.local(state)),
            ("core-new.local.toml", run_fixtures.CORE_LOCAL),
        ):
            with open(os.path.join(self.agent_pm, name), "w") as f:
                f.write(text)
        self.fake = fake_linear.FakeLinear(harness=HARNESS)
        self.ids = {HARNESS_EMAIL: self.fake.user(HARNESS_EMAIL, "Harness", HARNESS)}
        for r in ROLES:
            self.ids[ACCOUNTS[r]] = self.fake.user(ACCOUNTS[r], r.title(), KEYS[r])
        self.ids[HUMAN] = self.fake.user(HUMAN, "Me")
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
            **self.base(),
            **child_env({}, ORCH_ROOT),
            "TZ": "UTC",
            "AGENT_PM_PATH": self.bin,
            "CLAUDE_CONFIG_DIR": os.path.join(self.root, "claude-config"),
            linear.SEAM: self.seam,
            fake_claude.ENV: self.claude_scenario,
        }

    def guard(self):
        """config.path() must find the fake claude first: never a real one."""
        res = self.script("-c", GUARD)
        self.assertEqual(
            (res.returncode, res.stdout), (0, os.path.join(self.bin, "claude") + "\n"), res.stderr
        )

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

    def problems(self):
        return [(r.op, r.account, r.result) for r in self.requests() if r.result != "ok"]

    def email(self, uid):
        return uid and self.fake.users[uid]["email"]

    def issue(self, ident):
        with self.fake.lock:
            return json.loads(json.dumps(self.fake.find(ident)))

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
