"""delegate/cli.py as a subprocess: Integration over
session/tests/integration/adapters/claude/fake_claude.py, CodexIntegration over
session/tests/integration/adapters/codex/fake_codex.py, their outcomes.
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from delegate.tests.case import PY, REPO, Case, child_env
from session.tests.integration.adapters.claude import fake_claude
from session.tests.integration.adapters.codex import fake_codex

CLI = (*PY, "-m", "agent_pm.delegate.cli")
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
WAIT = 30  # seconds
RESETS = 1760000000
DONE = {"status": "done", "title": "echo", "summary": "Hello.", "deliverable": "Hello.\n"}
ASKING = {
    "status": "needs_input",
    "title": "echo",
    "summary": "Asking.",
    "questions": ["Which greeting?"],
}
UNSAVED = {k: v for k, v in DONE.items() if k != "deliverable"}
UNSAVED_ERROR = "done with a local destination must carry the deliverable"


class Runs(Case):
    """A CLI run's records in `work` and the fake client's calls in `log`."""

    def records(self) -> list[dict]:
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def last(self, kind: str) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]

    def calls(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()


class Integration(Runs):
    """ROLE over the fake `claude`: a HOME of the test's own, the cwd `proj`, workdir `work`, --sid
    SID.
    """

    ROLE = "dummy-tester"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.proj, self.work, bin_dir = (
            os.path.join(self.root, d) for d in ("proj", "work", "bin")
        )
        for d in (self.proj, self.work, bin_dir):
            os.mkdir(d)
        self.out = os.path.join(self.work, "out.md")
        self.scenario, self.log = (
            os.path.join(self.root, "scenario.json"),
            os.path.join(self.root, "log.jsonl"),
        )
        self.config = os.path.join(self.root, "config")
        self.env = {
            **child_env({}),
            "PATH": fake_claude.install(bin_dir),
            "HOME": self.home,
            "CLAUDE_CONFIG_DIR": self.config,
            "PYTHONUTF8": "1",
            "TZ": "UTC",
            fake_claude.ENV: self.scenario,
        }
        self.transcript = os.path.join(
            self.config, "projects", re.sub(r"[^A-Za-z0-9]", "-", self.proj), f"{SID}.jsonl"
        )

    def argv(self, *extra) -> list[str]:
        return [
            *CLI,
            "--role",
            self.ROLE,
            "--input",
            "Hello.",
            "--out",
            self.out,
            "--workdir",
            self.work,
            "--sid",
            SID,
            *extra,
        ]

    def scene(self, steps, **scenario) -> None:
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, **scenario, "log": self.log}, f)

    def drive(self, steps, *extra, cwd=None, argv=None, **scenario) -> subprocess.CompletedProcess:
        self.scene(steps, **scenario)
        return subprocess.run(
            argv or self.argv(*extra),
            cwd=cwd or self.proj,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def users(self) -> list:
        with open(self.transcript) as f:
            return [
                line["message"]["content"] for line in map(json.loads, f) if line["type"] == "user"
            ]

    def popen(self, *extra) -> subprocess.Popen:
        # A suite started in the background inherits SIGINT ignored; the CLI installs no handler of
        # its own.
        proc = subprocess.Popen(
            self.argv(*extra),
            cwd=self.proj,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
        )
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc: subprocess.Popen) -> None:
        targets = [(os.killpg, proc.pid)] + [(os.kill, call["pid"]) for call in self.calls()]
        for kill, target in targets:
            try:
                kill(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        proc.communicate()

    def until(self, proc: subprocess.Popen, done, what: str) -> None:
        deadline = time.monotonic() + WAIT
        while not done():
            if proc.poll() is not None or time.monotonic() > deadline:
                self.fail(f"the CLI never {what} (rc {proc.poll()})")
            time.sleep(0.05)


NATIVE = fake_codex.NATIVE
CODEX_END = {"out": {"type": "turn.completed"}}


def codex_said(*texts) -> list[dict]:
    """The fake's steps: an agent message per text, then the turn's end."""
    return [*map(fake_codex.message, texts), CODEX_END]


class CodexIntegration(Runs):
    """ROLE's TASK, --runtime codex over fake_codex; HOME outside the temp dirs."""

    ROLE, TASK = "dummy-tester", "echo"

    def setUp(self):
        home = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        tmp = tempfile.TemporaryDirectory()
        for d in (home, tmp):
            self.addCleanup(d.cleanup)
        self.home, root = os.path.realpath(home.name), os.path.realpath(tmp.name)
        self.work, bin_dir = os.path.join(root, "work"), os.path.join(root, "bin")
        for d in (self.work, bin_dir):
            os.mkdir(d)
        codex = os.path.join(bin_dir, "codex")
        with open(codex, "w") as f:
            f.write(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(fake_codex.__file__)} "$@"\n'
            )
        os.chmod(codex, 0o755)
        self.out = os.path.join(self.work, "out.md")
        self.scenario, self.log = (
            os.path.join(root, "scenario.json"),
            os.path.join(root, "exec.jsonl"),
        )
        self.env = {
            **child_env({}),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": self.home,
            "PYTHONUTF8": "1",
            "CODEX_HOME": os.path.join(self.home, ".codex"),
            "FAKE_LOG": self.log,
            "FAKE_SCENARIO": self.scenario,
        }

    def drive(self, steps, *extra, **scenario) -> subprocess.CompletedProcess:
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, **scenario}, f)
        argv = [
            *CLI,
            "--runtime",
            "codex",
            "--role",
            self.ROLE,
            "--task",
            self.TASK,
            "--input",
            "Hello.",
            "--out",
            self.out,
            "--workdir",
            self.work,
            "--sid",
            SID,
            *extra,
        ]
        return subprocess.run(
            argv,
            cwd=self.work,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def texts(self, kind: str, key: str = "text") -> list:
        return [e[key] for e in self.records() if e["kind"] == kind]
