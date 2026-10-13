"""Shared adapter fixtures for isolated session tests."""

import contextlib
import glob
import json
import os
import shlex
import subprocess
import sys
import tempfile

from session.tests import case, live_tmux
from session.tests.integration.adapters.claude import fake_claude

FAKE = os.path.abspath(fake_claude.__file__)

SID = "11111111-2222-4333-8444-555555555555"

SESSION = ("--session-id", SID)

ARGS = ["-p", "hi", *SESSION, "--output-format", "stream-json", "--verbose"]

RESUME = ["-p", "hi", "--resume", SID, "--output-format", "stream-json", "--verbose"]

TRUST = "Quick safety check: Is this a project you created or one you trust?"

LIMIT = "You've hit your session limit. Your limit resets 3pm (Europe/Berlin)"


def said(content) -> str:
    if isinstance(content, str):
        return content
    (block,) = content
    return block["text"] if block["type"] == "text" else f"{block['type']} {block['name']}"


def gone(pid: int) -> bool:
    res = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=30
    )
    return res.stdout.strip() == "" or res.stdout.strip().startswith("Z")


class Case(case.Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.proj, self.config = os.path.join(self.root, "proj"), os.path.join(self.root, "config")
        os.mkdir(self.proj)
        self.scenario, self.hooklog, self.log = (
            os.path.join(self.root, f) for f in ("scenario.json", "hooks.jsonl", "log.jsonl")
        )
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": self.root,
            "CLAUDE_CONFIG_DIR": self.config,
            fake_claude.ENV: self.scenario,
        }

    def scene(self, **scenario) -> None:
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump({"log": self.log, **scenario}, f)

    def tag(self, name: str) -> dict:
        return {"type": "command", "command": f"echo {name} >> {shlex.quote(self.hooklog)}"}

    def settings(self, *events: str) -> str:
        q = shlex.quote(self.hooklog)
        hook = {"type": "command", "command": f"cat >> {q}; echo >> {q}"}
        return json.dumps({"hooks": {e: [{"hooks": [hook]}] for e in events}})

    def run_fake(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return self.run_argv((sys.executable, "-I", FAKE, *args), stdin)

    def run_argv(self, argv: tuple[str, ...], stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            cwd=self.proj,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def popen(self, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-I", FAKE, *args],
            cwd=self.proj,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc: subprocess.Popen) -> None:
        proc.kill()
        proc.wait()
        for pipe in filter(None, (proc.stdin, proc.stdout, proc.stderr)):
            with contextlib.suppress(OSError):
                pipe.close()

    def logged(self) -> list[dict]:
        return [json.loads(line) for line in live_tmux.events(self.log)]

    def transcripts(self) -> list[str]:
        return glob.glob(os.path.join(self.config, "projects", "*", f"{SID}.jsonl"))

    def convo(self) -> list[tuple[str, str]]:
        (path,) = self.transcripts()
        with open(path, encoding="utf-8") as f:
            return [(r["type"], said(r["message"]["content"])) for r in map(json.loads, f)]

    def users(self) -> list[str]:
        return [text for role, text in self.convo() if role == "user"]

    def fired(self) -> list[dict]:
        return [json.loads(line) for line in live_tmux.events(self.hooklog) if line]

    def names(self) -> list[str]:
        return [h["hook_event_name"] for h in self.fired()]

    def result(self, res: subprocess.CompletedProcess) -> str:
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout.splitlines()[-1])["result"]
