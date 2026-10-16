"""Detached runs on PtyHost (FR-47, 356-16) through agent-pm-delegate, over the fake claude: with
`pty` the [hosts] default, a tui and a headless run reach their outcome with nothing on tmux; with
[delegates] harness_host = "pty", the harness runs on a pty and the agent's tui on a private tmux
server (session/tests/live_tmux.py), and resume brings each back on its own Host.
"""

import json
import os
import subprocess
import tempfile
import unittest

from agent_pm.delegate.engine.types import Layout
from agent_pm.session import Host
from agent_pm.utils import Record, load_class

from delegate.tests.case import PY, PYTHONPATH, Case
from delegate.tests.integration.cli import DONE, RUN, SID
from delegate.tests.replies import block, mark
from session.tests import live_tmux
from session.tests.integration.clients.claude import fake_claude

ROLE = "dummy-tester"
TUI = f"{ROLE}-{SID[:8]}"
HARNESS = f"{TUI}-drive"
START = mark("start", "echoing")
WAIT = 60  # seconds: a run
DELEGATE = (*PY, "-m", "agent_pm.delegate.cli")
PTY = {"outer_terminal": ["TMUX", "TMUX_PANE"]}
PTY_HOST = load_class("agent_pm.session.hosts.pty:PtyHost", Host)


class PtyRuns(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.server = live_tmux.Server(self, self.home)
        self.server.start("mgr")
        self.proj, self.work = (os.path.join(self.dir, d) for d in ("proj", "work"))
        for directory in (self.proj, self.work):
            os.mkdir(directory)
        self.out = os.path.join(self.work, "out.md")
        self.events, self.scenario = (
            os.path.join(self.dir, f) for f in ("events", "scenario.json")
        )
        self.pty = PTY_HOST("pty", PTY)
        self.addCleanup(self.close)

    def configure(self, text: str) -> None:
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w", encoding="utf-8") as file:
            file.write(text)

    def steps(self, steps, turns=()) -> None:
        with open(self.scenario, "w", encoding="utf-8") as file:
            json.dump({"steps": steps, "turns": list(turns)}, file)

    def call(self, *argv: str) -> subprocess.CompletedProcess:
        res = subprocess.run(
            argv,
            cwd=self.proj,
            env=self.server.env(
                PYTHONPATH=PYTHONPATH,
                CLAUDE_CONFIG_DIR=os.path.join(self.dir, "config"),
                **{fake_claude.ENV: self.scenario},
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=WAIT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def detach(self, mode: str) -> Record:
        argv = [*RUN, "--role", ROLE, "--input", "Hello.", "--out", self.out, "--mode", mode]
        argv += ["--workdir", self.work, "--sid", SID, "--detach", "--events", self.events]
        return Record.parse(self.call(*argv).stdout)

    def close(self) -> None:
        if os.path.isfile(os.path.join(Layout(self.work).run, "delegate.json")):
            subprocess.run(
                [*DELEGATE, "close", self.work],
                env=self.server.env(PYTHONPATH=PYTHONPATH),
                capture_output=True,
                timeout=WAIT,
                check=False,
            )

    def on_tmux(self, name: str) -> bool:
        return self.server.tmux("has-session", "-t", f"={name}").returncode == 0

    def on_pty(self, name: str) -> bool:
        return name in self.pty.list() and self.pty.attach(name).alive()

    def event(self, name: str, *words: str) -> None:
        tail = " ".join((name, *words))
        live_tmux.wait(
            lambda: any(line.endswith(f" {tail}") for line in live_tmux.events(self.events)),
            WAIT,
            what=f"`{tail}`",
        )

    def hosts(self) -> tuple[str, str]:
        with open(os.path.join(Layout(self.work).run, "delegate.json"), encoding="utf-8") as file:
            found = json.load(file)
        return found["host"], found["harness_host"]


class PtyDefault(PtyRuns):
    def setUp(self):
        super().setUp()
        self.configure('[session.hosts]\ndefault = "pty"\n')

    def reaches_its_outcome(self, mode: str, sessions: tuple[str, ...]) -> None:
        record = self.detach(mode)
        self.assertEqual(record.sessions, sessions)
        self.event(HARNESS, "outcome", "done")
        with open(self.out, encoding="utf-8") as file:
            self.assertEqual(file.read(), DONE["deliverable"])
        self.assertEqual(self.hosts(), ("pty", "pty"))
        self.assertEqual(self.server.tmux("list-sessions", "-F", "#S").stdout.split(), ["mgr"])

    def test_a_tui_run_on_the_pty_host_reaches_its_outcome(self):
        self.steps([START], turns=[[block(DONE)]])
        self.reaches_its_outcome("tui", (HARNESS, TUI))
        self.event(TUI, "done")

    def test_a_headless_run_on_the_pty_host_reaches_its_outcome(self):
        self.steps([START, block(DONE)])
        self.reaches_its_outcome("headless", (HARNESS,))
        self.event(HARNESS, "exited", "0")


class PtyHarness(PtyRuns):
    def test_a_pty_harness_drives_a_tmux_tui_and_resume_brings_each_back_on_its_host(self):
        self.configure('[delegate.delegates]\nharness_host = "pty"\n')
        self.steps([START])
        record = self.detach("tui")
        self.assertEqual(record.sessions, (HARNESS, TUI))
        self.event(TUI, "done")
        self.assertTrue(self.on_pty(HARNESS) and self.on_tmux(TUI))
        self.assertFalse(self.on_tmux(HARNESS) or TUI in self.pty.list())
        self.pty.attach(HARNESS).close()
        self.server.tmux("kill-session", "-t", f"={TUI}")
        live_tmux.wait(
            lambda: not self.on_pty(HARNESS) and not self.on_tmux(TUI), WAIT, what="both gone"
        )
        resumed = Record.parse(self.call(*DELEGATE, "resume", self.work).stdout)
        self.assertEqual((resumed.sid, resumed.sessions), (record.sid, record.sessions))
        live_tmux.wait(lambda: self.on_pty(HARNESS) and self.on_tmux(TUI), WAIT, what="both back")
        self.assertEqual(self.hosts(), ("tmux", "pty"))


if __name__ == "__main__":
    unittest.main()
