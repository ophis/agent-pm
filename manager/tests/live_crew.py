"""agent-pm-crew over the real layer commands: a private tmux server (session/tests/live_tmux.py)
with manager MANAGER attached, <repo>/bin and the fake clients on PATH, HOME outside the temp
dirs (Codex's rule), and the crew run as a module with only utils and manager importable (FR-3).
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from unittest import mock

from agent_pm.manager import Entry, Roster
from agent_pm.utils import Record

from manager.tests.case import PY, REPO, SRC, Case
from session.tests import live_tmux
from session.tests.integration.adapters.claude import fake_claude

MANAGER = "mgr"
OTHER = "mgr2"
ROLE = "dummy-tester"
TIMEOUT = 90
CREW = (*PY, "-m", "agent_pm.manager.cli")
BIN = os.path.join(REPO, "bin")


def logged(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(a == flag and b == value for a, b in zip(argv, argv[1:], strict=False))


class LiveCrew(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home = os.path.join(self.root, "home")
        self.agent_pm = os.path.join(self.home, ".agent-pm")
        self.cwd = os.path.join(self.root, "work")
        for path in (self.agent_pm, self.cwd):
            os.makedirs(path)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": self.home}))
        self.spies = os.path.join(self.root, "spies")
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.events = self.events_of(MANAGER)

    def events_of(self, manager: str) -> str:
        return os.path.join(self.agent_pm, "managers", manager, "events")

    def roster(self, manager: str = MANAGER) -> dict[str, Entry]:
        return Roster(os.path.dirname(self.events_of(manager))).entries()

    def env(self, *, inside: str | None = MANAGER, **extra: str | None) -> dict[str, str]:
        """A caller's in session `inside` (None: outside tmux); an extra None drops its key."""
        env = self.server.env(**(self.server.inside(inside) if inside else {}))
        env["PATH"] = os.pathsep.join(
            (self.spies, BIN, self.server.bin, os.path.dirname(sys.executable), env["PATH"])
        )
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def call(
        self,
        *argv: str,
        inside: str | None = MANAGER,
        stdin: str = "",
        cwd: str | None = None,
        **extra: str | None,
    ) -> subprocess.CompletedProcess:
        with self.poking():
            return subprocess.run(
                argv,
                env=self.env(inside=inside, **extra),
                cwd=cwd or self.cwd,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
            )

    def crew(self, *args: str, manager: str = MANAGER, **kw) -> subprocess.CompletedProcess:
        crew_path = os.pathsep.join((SRC["utils"], SRC["manager"]))
        return self.call(*CREW, *args, AGENT_PM_MANAGER=manager, PYTHONPATH=crew_path, **kw)

    def session(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call("agent-pm-session", *args, **kw)

    def ok(self, result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(
            result.returncode, 0, f"{result.args}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return result

    def record(self, result: subprocess.CompletedProcess) -> Record:
        lines = self.ok(result).stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        return Record.parse(lines[0])

    def scene(self, name: str, steps=None, turns=()) -> dict[str, str]:
        """`name`'s fake claude scenario, as env; its starts log to self.log(name)."""
        path = os.path.join(self.root, f"{name}.claude.json")
        steps = [f"Hi from {name}."] if steps is None else steps
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"steps": steps, "turns": list(turns), "log": self.log(name)}, f)
        return {fake_claude.ENV: path}

    def log(self, name: str) -> str:
        return os.path.join(self.root, f"{name}.claude.jsonl")

    def worker(
        self,
        name: str,
        *flags: str,
        steps=None,
        turns=(),
        note: str | None = None,
        client: str = "claude",
        **kw,
    ) -> subprocess.CompletedProcess:
        """crew run `name` -- agent-pm-session start `name`, a fake claude worker in self.cwd."""
        noted = ("--note", note) if note else ()
        start = ("agent-pm-session", "start", name, "--client", client, "--cwd", self.cwd)
        return self.crew(
            "run",
            name,
            *noted,
            "--",
            *start,
            "--prompt",
            "hi",
            *flags,
            **{**self.scene(name, steps, turns), **kw},
        )

    def role(
        self, name: str, *flags: str, steps, turns=(), mode: str = "tui", **kw
    ) -> subprocess.CompletedProcess:
        """crew run `name` -- agent-pm-delegate run … --detach, ROLE over the fake claude; its
        workdir self.workdir(name), no --sid or --events.
        """
        work = self.workdir(name)
        os.makedirs(work, exist_ok=True)
        run = ("agent-pm-delegate", "run", "--role", ROLE, "--input", "Hello.")
        detach = ("--out", os.path.join(work, "out.md"), "--workdir", work, "--mode", mode)
        return self.crew(
            "run",
            name,
            "--",
            *run,
            *detach,
            "--detach",
            *flags,
            **{**self.scene(name, steps, turns), **kw},
        )

    def workdir(self, name: str) -> str:
        return os.path.join(self.root, f"{name}-work")

    def spy(self, command: str) -> str:
        """Puts a `command` before <repo>/bin that logs its argv, then runs the real one; its log."""
        os.makedirs(self.spies, exist_ok=True)
        log = os.path.join(self.root, f"{command}.calls")
        with open(os.path.join(self.spies, command), "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{BIN}/{command}" "$@"\n')
        os.chmod(os.path.join(self.spies, command), 0o755)
        return log

    def wait_event(self, name: str, word: str, count: int = 1, events: str | None = None):
        def found():
            self.poke()
            lines = live_tmux.events(events or self.events)
            hits = [line for line in lines if line.split(" ")[1:3] == [name, word]]
            return lines if len(hits) >= count else None

        return live_tmux.wait(found, TIMEOUT, what=f"{count} x `{name} {word}`")

    def until(self, check, what: str):
        def poked():
            self.poke()
            return check()

        return live_tmux.wait(poked, TIMEOUT, what=what)

    def live(self, *names: str) -> bool:
        return all(self.server.tmux("has-session", "-t", f"={n}").returncode == 0 for n in names)

    def gone(self, *names: str) -> bool:
        return not any(self.live(n) for n in names)

    def kill(self, *names: str) -> None:
        for name in names:
            self.ok(self.server.tmux("kill-session", "-t", f"={name}"))
        self.until(lambda: self.gone(*names), f"{names} gone")

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
