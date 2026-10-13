"""Fixtures for tui tests."""

import fcntl
import json
import os
import re
import tempfile
from unittest import mock

from agent_pm.session.engine.types import (
    Key,
    LimitHit,
    Paste,
    SessionInfo,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.tui import types as tui

from session.tests import fake_adapter
from session.tests.case import Case

NAME = "w1"

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"

TMUX = "/opt/tmux/bin/tmux"

TS = "2026-10-11T12:00:00.000+00:00"

WORDS = {"stop": "done", "ask": "blocked", "tool": "working"}

DEFAULT = object()


def record(label: str, **payload) -> dict:
    return {"ts": TS, "label": label, "payload": payload}


def fake_host() -> mock.Mock:
    """tmux_host autospecced, its constants, TuiError and attach_command real: status None (absent),
    read "", option None, pane %1.
    """
    host = mock.create_autospec(tmux_host)
    for name in ("RUNNING", "UNKNOWN", "PAUSE", "TuiError"):
        setattr(host, name, getattr(tmux_host, name))
    host.attach_command.side_effect = tmux_host.attach_command
    host.status.return_value = None
    host.read.return_value = ""
    host.option.return_value = None
    host.pane.return_value = "%1"
    return host


class Clock:
    """time.monotonic and time.sleep's stand-in: sleep moves `now`, then runs each action due by
    then; every sleep is logged in `log`.
    """

    def __init__(self, log: list):
        self.now, self.log, self.actions = 0.0, log, []

    def sleep(self, seconds: float) -> None:
        self.log.append(("sleep", seconds))
        self.now += seconds
        while self.actions and self.actions[0][0] <= self.now:
            self.actions.pop(0)[1]()


class ScanTui(fake_adapter.FakeTui):
    """The fake Tui with two dialogs and a usage-limit message `LIMIT <epoch>`; activity is the
    payload's tool, the blocked reason its why; each screen limit() sees is kept.
    """

    dialogs = ("Trust  this\nfolder?", "Sign in")

    def __init__(self, adapter):
        super().__init__(adapter)
        self.screens = []

    def limit(self, screen):
        self.screens.append(screen)
        found = re.search(r"LIMIT ([0-9]+)", screen)
        return LimitHit(float(found[1])) if found else None

    def activity(self, payload):
        return payload.get("tool", "")

    def blocked_reason(self, payload):
        return payload.get("why", "")


class Base(Case):
    def setUp(self):
        self.root = os.path.join(self.agent_pm, "tui")
        self.dir = os.path.join(self.root, NAME)
        self.hooks = os.path.join(self.dir, "hooks.jsonl")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.events = os.path.join(self.tmp, "events")
        self.host = fake_host()
        self.log = []
        self.clock = Clock(self.log)
        for patch in (
            mock.patch("time.monotonic", lambda: self.clock.now),
            mock.patch("time.sleep", self.clock.sleep),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.run = ScanTui(fake_adapter.FakeAdapter({"tui": True}))
        self.info = SessionInfo(
            sid=SID, native_id="", cwd=self.tmp, project=False, transcript="", resume=""
        )

    def prepare(self, run=None, info=None, events=DEFAULT, workdir=None, tmux=TMUX) -> tui.TuiMode:
        events = self.events if events is DEFAULT else events
        return tui.TuiMode.prepare(
            NAME,
            run or self.run,
            info or self.info,
            setup=tui.TuiSetup(
                runtime="fake", workdir=workdir or self.tmp, tmux=tmux, events=events
            ),
            host=self.host,
        )

    def started(self) -> tui.TuiMode:
        """A TuiMode prepared (the session absent), begun on pane %1, the host's calls cleared, its
        status RUNNING.
        """
        self.host.status.return_value, self.host.pane.return_value = None, "%1"
        mode = self.prepare()
        mode.begin(("/bin/run",), cwd=self.tmp, env={})
        self.host.reset_mock()
        self.host.status.return_value = tmux_host.RUNNING
        return mode

    def append(self, *lines) -> None:
        """Each line to hooks.jsonl: bytes as given, else a JSON line."""
        with open(self.hooks, "ab") as f:
            for line in lines:
                f.write(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())

    def state(self, **changes) -> dict:
        return {
            "runtime": "fake",
            "sid": SID,
            "workdir": self.tmp,
            "cwd": self.tmp,
            "project": False,
            "transcript": "",
            "resume": "",
            "events": self.events,
            "words": WORDS,
            "tmux_path": TMUX,
            **changes,
        }

    def write_state(self, value) -> None:
        os.makedirs(self.dir, 0o700, exist_ok=True)
        with open(os.path.join(self.dir, "session.json"), "wb") as f:
            f.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    def assertNoSessionDir(self):
        self.assertFalse(os.path.lexists(self.dir))

    def locked(self, name: str = NAME) -> bool:
        try:
            fd = os.open(os.path.join(self.root, f"{name}.lock"), os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False


class Keys(ScanTui):
    def submit(self, text):
        return Key("Escape"), Paste(text), Key("Enter"), Key("Enter"), Paste("b"), Key("Tab")
