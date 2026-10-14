"""agent-pm-session as its users run it: bin/agent-pm-session in a subprocess (no PYTHONPATH; the
running interpreter's directory supplies python3), a fresh cwd and HOME, a private tmux server
(live_tmux.Server, a control-mode client on MANAGER) and the fake claude on PATH. `module=True` runs
`python3 -P -m agent_pm.session.cli` with this tree's roots instead, for a config naming a test
Host (session.tests.fakes.host:FakeHost, not Displayable).
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager

from agent_pm.utils import Record, Status

from session.tests import live_tmux
from session.tests.case import REPO, ROOTS, Case
from session.tests.integration.adapters.claude import fake_claude

COMMAND = os.path.join(REPO, "bin", "agent-pm-session")
MANAGER = "mgr"
TIMEOUT = 60
FAKE_HOST = "session.tests.fakes.host:FakeHost"
CLAUDE_HOOKS = {"Stop", "PermissionRequest", "Notification", "PostToolUse", "UserPromptSubmit"}
POLICY = ("--permission-mode", "--allowedTools", "--add-dir", "--dangerously-skip-permissions")


def logged(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


class CliCase(Case):
    ATTACH = True

    def setUp(self):
        parent = os.environ.get("AGENT_PM_TEST_TMP")
        tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.cwd = os.path.join(self.root, "work")
        os.mkdir(self.cwd)
        self.events = os.path.join(self.root, "events")
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        if self.ATTACH:
            self.server.attach(MANAGER)

    def env(self, *, inside: str | None = MANAGER, **extra: str | None) -> dict[str, str]:
        """The env a caller in session `inside` (None: outside tmux) runs the command with; an
        `extra` value None drops that key.
        """
        env = self.server.env(**(self.server.inside(inside) if inside else {}))
        env["PATH"] = os.pathsep.join(
            (self.server.bin, os.path.dirname(sys.executable), env["PATH"])
        )
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def session(
        self,
        *argv: str,
        stdin: str | None = None,
        inside: str | None = MANAGER,
        cwd: str | None = None,
        module: bool = False,
        **extra: str | None,
    ) -> subprocess.CompletedProcess:
        env = self.env(inside=inside, **extra)
        if module:
            env["PYTHONPATH"] = os.pathsep.join((*ROOTS, REPO))
            command = [sys.executable, "-P", "-m", "agent_pm.session.cli", *argv]
        else:
            command = [COMMAND, *argv]
        with self.poking():
            return subprocess.run(
                command,
                env=env,
                cwd=cwd or self.cwd,
                input="" if stdin is None else stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )

    def ok(self, result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(
            result.returncode, 0, f"{shlex.join(result.args)}\n{result.stdout}\n{result.stderr}"
        )
        return result

    def scene(self, name: str, steps=None, turns=(), **more) -> dict[str, str]:
        """The fake claude's scenario for worker `name` (its log: self.log(name)), as env."""
        path = os.path.join(self.root, f"{name}.claude.json")
        steps = [f"Hi from {name}."] if steps is None else steps
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"steps": steps, "turns": list(turns), "log": self.log(name), **more}, f)
        return {"FAKE_CLAUDE_SCENARIO": path}

    def log(self, name: str) -> str:
        return os.path.join(self.root, f"{name}.claude.jsonl")

    def calls(self, name: str) -> list[dict]:
        """The fake claude's starts for worker `name`: pid, argv, cwd."""
        return logged(self.log(name))

    def start(
        self, name: str, *args: str, steps=None, turns=(), prompt: str = "hi", **extra
    ) -> subprocess.CompletedProcess:
        """start NAME --runtime claude --cwd self.cwd --prompt PROMPT ARGS…, its events
        self.events.
        """
        env = {"AGENT_PM_EVENTS": self.events, **self.scene(name, steps, turns), **extra}
        return self.session(
            "start",
            name,
            "--runtime",
            "claude",
            "--cwd",
            self.cwd,
            "--prompt",
            prompt,
            *args,
            **env,
        )

    def record(self, result: subprocess.CompletedProcess) -> Record:
        """The one Record line a start, restart or resume printed."""
        self.ok(result)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        return Record.parse(lines[0])

    def status(self, *names: str, stdin: str | None = None, **kw) -> list[Status]:
        result = self.ok(self.session("status", *names, stdin=stdin, **kw))
        return [Status.parse(line) for line in result.stdout.splitlines()]

    def wait_event(self, name: str, word: str, count: int = 1, events: str | None = None):
        def found():
            lines = live_tmux.events(events or self.events)
            hits = [line for line in lines if line.split(" ")[1:3] == [name, word]]
            return lines if len(hits) >= count else None

        return live_tmux.wait(found, what=f"{count} x {name} {word}")

    def poke(self) -> None:
        # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so no pane-died, until
        # another child of the server exits: a no-op job is one
        self.server.tmux("run-shell", "-b", "true")

    @contextmanager
    def poking(self):
        stop = threading.Event()

        def run():
            while not stop.wait(live_tmux.POLL * 4):
                self.poke()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def local_config(self, text: str) -> None:
        """~/.agent-pm/config.local.toml (sections per layer: [session…])."""
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def fake_host(self, default: str = "tmux") -> str:
        """Configures a second Host, `fake` (FakeHost, not Displayable), and `default`; its root."""
        root = os.path.join(self.root, "fake-host")
        self.local_config(
            f'[session.hosts]\ndefault = "{default}"\n[session.hosts.fake]\n'
            f'implementation = "{FAKE_HOST}"\nroot = "{root}"\n'
        )
        return root


ENV_DUMP = (
    "import json, os, sys; open(sys.argv[1], 'a')"
    ".write(json.dumps({'argv': sys.argv[2:], 'env': dict(os.environ)}) + '\\n')"
)


class WorkerCase(CliCase):
    def dumping(self) -> str:
        """Puts a `claude` on PATH that logs its argv and env, then runs the fake; that log."""
        path = os.path.join(self.root, "claude.env.jsonl")
        q = shlex.quote
        with open(os.path.join(self.server.bin, "claude"), "w", encoding="utf-8") as f:
            f.write(
                f'#!/bin/sh\n{q(sys.executable)} -I -c {q(ENV_DUMP)} {q(path)} "$@"\n'
                f'exec {q(sys.executable)} -I {q(fake_claude.__file__)} "$@"\n'
            )
        return path

    def claude_argv(self, argv: list[str], sid: str, name: str, *tail: str) -> None:
        """argv is a worker's: its sid, name, settings sources and hooks, then `tail`."""
        self.assertEqual(
            argv[:7] + argv[8:],
            [
                "--session-id",
                sid,
                "--name",
                name,
                "--setting-sources",
                "user,project,local",
                "--settings",
                *tail,
            ],
        )
        settings = json.loads(argv[7])
        self.assertEqual(set(settings), {"hooks"})
        self.assertEqual(set(settings["hooks"]), CLAUDE_HOOKS)
        self.assertFalse([word for word in argv if word.startswith(POLICY)], argv)

    def live(self, name: str) -> bool:
        return self.server.tmux("has-session", "-t", f"={name}").returncode == 0

    def pane(self, name: str, fmt: str) -> str:
        res = self.server.tmux("display-message", "-p", "-t", f"={name}:", fmt)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.removesuffix("\n")

    def words(self) -> list[list[str]]:
        return [line.split(" ")[1:] for line in live_tmux.events(self.events)]
