"""Shared case fixtures for this topic."""

import contextlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
from unittest import mock

from agent_pm.manager.engine import monitor, roster, workers
from agent_pm.manager.engine.types import Entry

from manager.tests.case import PYTHONPATH, REPO, Case
from manager.tests.integration.crew.data import (
    CREW,
    ENTRY,
    ENV_DUMP,
    MANAGER,
    ROLE,
    ROLE_DRIVER,
    ROLE_SID,
    ROLE_TUI,
    SECOND,
    TIMEOUT,
    TMUX_HOST,
    is_event,
    logged,
    tail,
)
from session.tests import live_tmux
from session.tests.integration.adapters.claude import fake_claude
from session.tests.integration.adapters.codex import fake_tui


class Live(Case):
    ATTACH = True

    def setUp(self):
        parent = os.environ.get("AGENT_PM_TEST_TMP", REPO)
        tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home = os.path.join(self.root, "home")
        self.agent_pm = os.path.join(self.home, ".agent-pm")
        self.cwd = os.path.join(self.root, "work")
        for path in (self.agent_pm, self.cwd):
            os.makedirs(path)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": self.home}))
        mkdtemp = tempfile.mkdtemp
        with mock.patch.object(
            live_tmux.tempfile, "mkdtemp", side_effect=lambda: mkdtemp(dir=parent)
        ):
            self.server = live_tmux.Server(self, self.home)
        fake_tui.install(self.server.bin)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.events = self.events_of(MANAGER)
        self.directory = os.path.dirname(self.events)
        self.codex_home = os.path.join(self.home, ".codex")
        if self.ATTACH:
            self.attach()

    def events_of(self, manager: str) -> str:
        return os.path.join(self.agent_pm, "managers", manager, "events")

    def ok(self, res: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(
            res.returncode, 0, f"{res.args}\nstdout: {res.stdout}\nstderr: {res.stderr}"
        )
        return res

    def poke(self) -> None:
        # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so no pane-died, until
        # another child of the server exits: a no-op job is one.
        self.server.tmux("run-shell", "-b", "true")

    @contextlib.contextmanager
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

    def call(
        self, *argv: str, inside: str | None = MANAGER, cwd: str | None = None, **extra: str
    ) -> subprocess.CompletedProcess:
        env = self.server.env(**(self.server.inside(inside) if inside else {}), **extra)
        with self.poking():
            return subprocess.run(
                list(argv),
                env=env,
                cwd=cwd or self.cwd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=TIMEOUT,
            )

    def workers(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call(*CREW, *args, **{"PYTHONPATH": PYTHONPATH, **kw})

    def attach(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.ok(self.workers("attach", *args, **kw))

    def send(self, name: str, text: str, **kw) -> subprocess.CompletedProcess:
        return self.ok(self.call(sys.executable, TMUX_HOST, "send", name, text, **kw))

    def kill(self, name: str) -> subprocess.CompletedProcess:
        return self.ok(self.server.tmux("kill-session", "-t", f"={name}"))

    def scene(self, name: str, steps: list | None = None, turns=(), **codex) -> dict[str, str]:
        """Both fakes' scenario files for worker `name`, as the env a crew call passes on to its
        worker.
        """
        steps = [f"Hi from {name}."] if steps is None else steps
        claude, scenario = (
            os.path.join(self.root, f"{name}.{k}.json") for k in ("claude", "codex")
        )
        with open(claude, "w") as f:
            json.dump({"steps": steps, "turns": list(turns), "log": self.claude_log(name)}, f)
        with open(scenario, "w") as f:
            json.dump({"tui_turns": [steps, *turns], **codex}, f)
        return {
            fake_claude.ENV: claude,
            "FAKE_SCENARIO": scenario,
            "FAKE_LOG": self.codex_log(name),
        }

    def claude_log(self, name: str) -> str:
        return os.path.join(self.root, f"{name}.claude.jsonl")

    def codex_log(self, name: str) -> str:
        return os.path.join(self.root, f"{name}.codex.jsonl")

    def start(
        self,
        name: str,
        *args: str,
        steps: list | None = None,
        turns=(),
        inside: str | None = MANAGER,
        codex: dict | None = None,
        **extra: str,
    ) -> subprocess.CompletedProcess:
        return self.workers(
            "start",
            name,
            "--cwd",
            self.cwd,
            "--prompt",
            "hi",
            *args,
            inside=inside,
            **self.scene(name, steps, turns, **(codex or {})),
            **extra,
        )

    def started(self, name: str, *args: str, **kw) -> str:
        res = self.ok(self.start(name, *args, **kw))
        m = re.fullmatch(rf"{re.escape(name)} ({roster.SID.pattern})\n", res.stdout)
        self.assertIsNotNone(m, res.stdout)
        return m[1]

    def restart(self, name: str, steps: list, **extra: str) -> subprocess.CompletedProcess:
        return self.ok(self.workers("restart", name, **self.scene(name, steps), **extra))

    def calls(self, name: str) -> list[dict]:
        return logged(self.claude_log(name))

    def codex_calls(self, name: str) -> list[dict]:
        return [e for e in logged(self.codex_log(name)) if e.get("mode") == "tui"]

    def native(self, name: str) -> str:
        (native,) = {
            e["payload"]["session_id"] for e in logged(self.codex_log(name)) if "hook" in e
        }
        return native

    def next_event(self, after: int, *args: str, gen: int = 0, **kw) -> tuple[int, str, str]:
        out = self.ok(
            self.workers("next-event", *args, "--after", str(after), "--gen", str(gen), **kw)
        ).stdout
        head, _, content = out.partition("\n")
        n, _, event = head.partition(" ")
        return int(n), event, content.removesuffix("\n")

    def wait_event(
        self, name: str, kind: str, count: int = 1, events: str | None = None
    ) -> list[str]:
        path = events or self.events

        def check():
            self.poke()
            lines = live_tmux.events(path)
            return lines if sum(is_event(line, name, kind) for line in lines) >= count else None

        return live_tmux.wait(check, what=f"{count} `{name} {kind}` events in {path}")

    def record(self, sid: str) -> dict:
        path = os.path.join(self.agent_pm, "workers", sid, workers.RECORD)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        with open(path) as f:
            return json.load(f)

    def roster(self, directory: str | None = None) -> dict:
        with open(os.path.join(directory or self.directory, "roster.json")) as f:
            return json.load(f)

    def display(self, name: str, fmt: str) -> str:
        return self.ok(
            self.server.tmux("display-message", "-p", "-t", f"={name}:", fmt)
        ).stdout.removesuffix("\n")

    def pane_text(self, name: str, lines: int) -> str:
        text = self.ok(
            self.server.tmux("capture-pane", "-p", "-J", "-t", f"={name}:", "-S", f"-{lines}")
        ).stdout
        rows = text.splitlines()
        while rows and not rows[-1].strip():
            rows.pop()
        return "\n".join(rows[-lines:])

    def shown(self, name: str) -> str:
        """The pane showing `name` in the manager's window."""
        pane = self.server.pane_of(name)
        self.assertIn(pane, self.server.panes(f"={MANAGER}:"))
        return pane

    def grid(self, columns: list) -> None:
        def check():
            return live_tmux.assert_grid(self, self.server, MANAGER, columns)

        with contextlib.suppress(AssertionError):
            live_tmux.wait(check)
        check()

    def live(self, *names: str) -> bool:
        return all(self.server.tmux("has-session", "-t", f"={n}").returncode == 0 for n in names)

    def gone(self, *names: str) -> bool:
        return not any(self.live(n) for n in names)

    def dumping(self, runtime: str, fake: str) -> str:
        """
        Puts a `runtime` on PATH that logs its argv and env, then runs `fake`; returns that log.
        """
        path = os.path.join(self.root, f"{runtime}.env.jsonl")
        q = shlex.quote
        with open(os.path.join(self.server.bin, runtime), "w") as f:
            f.write(
                f'#!/bin/sh\n{q(sys.executable)} -I -c {q(ENV_DUMP)} {q(path)} "$@"\n'
                f'exec {q(sys.executable)} -I {q(fake)} "$@"\n'
            )
        return path

    def second(self) -> None:
        self.ok(self.server.tmux("new-session", "-d", "-s", SECOND, *live_tmux.IDLE))
        self.server.attach(SECOND)

    def role(self, work: str, *sessions: str) -> dict:
        """A role run's entry in the manager's roster, as drive.py --detach writes one, and its
        sessions.
        """
        for session in sessions:
            self.ok(self.server.tmux("new-session", "-d", "-s", session, *live_tmux.IDLE))
        resume = [sys.executable, os.path.join(self.root, "drive.py"), f"--role={ROLE}"]
        e = roster.entry(Entry("role", sid=ROLE_SID, cwd=work, resume=resume, tui=ROLE_TUI))
        with roster.locked(self.directory) as record:
            roster.record(record, ROLE_DRIVER, e)
        return e

    def recover(self, name: str, **extra: str) -> subprocess.CompletedProcess:
        e = self.roster()["entries"][name]
        return self.call(*roster.recovery(e), cwd=e["cwd"], PYTHONPATH=PYTHONPATH, **extra)

    def worker_entry(self, name: str, sid: str, *rest: str, client: str = "claude") -> list[str]:
        return [
            *ENTRY,
            "start",
            name,
            "--manager",
            MANAGER,
            "--client",
            client,
            "--resume",
            sid,
            "--cwd",
            self.cwd,
            *rest,
        ]


class RosterLive(Live):
    ATTACH = False

    def held(self, holder: str) -> str:
        h = self.roster()["holder"]
        self.assertEqual(h["session"], holder)
        return (
            f"workers: manager directory {self.directory}: held by tmux session {holder} "
            f"since {h['since']}; ask "
            "that manager to run crew.py release, or end that session\n"
        )

    def refused(self) -> None:
        before = self.roster()
        res = self.workers("attach", "--manager", MANAGER, inside=SECOND)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", self.held(MANAGER)))
        self.assertEqual(self.roster(), before)

    def taken_over(self) -> None:
        res = self.attach("--manager", MANAGER, inside=SECOND)
        self.assertEqual(res.stdout, f"{monitor.HEADER}\n{tail(self.directory)}")
        self.assertEqual(self.roster()["holder"]["session"], SECOND)

    def after_w1(self, out: str, rest: str) -> None:
        self.assertRegex(
            out, rf"\A{re.escape(monitor.HEADER)}\nw1\tworker\t[^\n]*\n{re.escape(rest)}\Z"
        )
