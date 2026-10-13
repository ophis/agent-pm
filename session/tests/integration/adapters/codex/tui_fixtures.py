"""Shared native runtime fixtures for adapter integration tests."""

import dataclasses
import json
import os
import queue
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from agent_pm.session import Driver, StartOptions
from agent_pm.session.adapters.codex.sessions import store_root
from agent_pm.session.engine.types import (
    Access,
    Exited,
    Spec,
)

from session.tests import live_tmux
from session.tests.case import PY, REPO, Case, child_env, settings
from session.tests.integration.adapters.codex import fake_tui

WAIT = 15

CHANGED = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

ATTACH = """import json, sys
from agent_pm.session.engine import api
from agent_pm.session import Driver, TurnEnd
driver = Driver(**json.loads(sys.argv[1]))
h = driver.attach(sys.argv[2])
before = [h.sid, h.state(), h.reply, h.info().native_id]
if sys.argv[3]:
    h.send(sys.argv[3])
    turn = next(e for e in h.events() if isinstance(e, TurnEnd))
    print(json.dumps([before, [turn.reply, turn.pending], h.reply]))
else:
    print(json.dumps(before))
"""


class StreamError(AssertionError):
    @classmethod
    def incomplete_consumer(cls):
        return cls("event consumer did not finish")

    @classmethod
    def event_deadline(cls):
        return cls("no event before deadline")

    @classmethod
    def unexpected_event(cls, event):
        return cls(f"unexpected event: {event!r}")


class Stream:
    def __init__(self, case, handle):
        self.queue = queue.Queue()
        self.server = case.server
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.result = self.executor.submit(self.collect, handle)
        case.addCleanup(self.close, handle)

    def collect(self, handle):
        for event in handle.events():
            self.queue.put(event)

    def close(self, handle):
        handle.stop()
        try:
            self.result.result(timeout=2)
        except TimeoutError:
            raise StreamError.incomplete_consumer() from None
        finally:
            self.executor.shutdown(wait=False, cancel_futures=True)

    def next(self):
        deadline = time.monotonic() + WAIT
        while True:
            try:
                event = self.queue.get(timeout=live_tmux.POLL)
                break
            except queue.Empty:
                if self.result.done():
                    self.result.result()
                self.server.tmux("run-shell", "-b", "true")
                if time.monotonic() > deadline:
                    raise StreamError.event_deadline() from None
        return event

    def until(self, kind):
        found = [self.next()]
        while not isinstance(found[-1], kind):
            found.append(self.next())
        return found

    def quiet(self):
        try:
            raise StreamError.unexpected_event(self.queue.get(timeout=0.4))
        except queue.Empty:
            pass


class Live(Case):
    def setUp(self):
        parent = os.environ.get("AGENT_PM_TEST_TMP", REPO)
        tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.work, self.extra = (self.root / n for n in ("home", "work", "extra"))
        self.cwd, self.codex_home = self.work / "project", self.home / ".codex"
        for path in (self.home, self.cwd, self.codex_home, self.extra):
            path.mkdir(parents=True, exist_ok=True)
        mkdtemp = tempfile.mkdtemp
        with mock.patch.object(
            live_tmux.tempfile, "mkdtemp", side_effect=lambda: mkdtemp(dir=parent)
        ):
            self.server = live_tmux.Server(self, str(self.home))
        self.server.start("mgr")
        fake_tui.install(self.server.bin)
        self.log, self.scenario, self.events = (
            self.root / n for n in ("runtime.jsonl", "scenario.json", "events")
        )
        self.scene()
        self.env = self.server.env(CODEX_HOME=str(self.codex_home), TZ="UTC")
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))
        self.config = settings(
            {
                "codex": {
                    "implementation": "adapters.codex:CodexAdapter",
                    "tiers": {"1": "gpt-test"},
                    "efforts": {"low": "low"},
                    "env": {
                        "FAKE_LOG": str(self.log),
                        "FAKE_SCENARIO": str(self.scenario),
                        "TZ": "UTC",
                    },
                }
            }
        )
        self.driver = Driver(**self.config)
        self.common = self.driver.registry.adapter("codex").common
        self.spec = Spec(
            flags=(
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ),
            workdir=str(self.work),
            tier=1,
            effort="low",
            access=Access(
                cwd=str(self.cwd), project=True, dirs=(str(self.extra),), commands=("git status",)
            ),
        )
        self.sid = "caller-id"

    def scene(self, turns=None, **fields):
        self.scenario.write_text(
            json.dumps({"tui_turns": [["first"]] if turns is None else turns, **fields})
        )

    def start(self, name="w1", prompt="hello", **kwargs):
        handle = self.driver.start(
            "codex",
            "tui",
            prompt,
            dataclasses.replace(
                kwargs.pop("spec", self.spec), name=name, sid=kwargs.pop("sid", self.sid)
            ),
            options=StartOptions(**{"events": str(self.events), **kwargs}),
        )
        self.addCleanup(handle.stop)
        return handle

    def logged(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def calls(self, mode="tui"):
        return [row for row in self.logged() if row.get("mode") == mode]

    def hooks(self):
        return [row for row in self.logged() if "hook" in row]

    def lines(self, count):
        def check():
            self.server.tmux("run-shell", "-b", "true")
            rows = live_tmux.events(str(self.events))
            return rows if len(rows) >= count else None

        return [
            line.split(" ", 1)[1]
            for line in live_tmux.wait(check, WAIT, what=f"{count} event lines")
        ]

    def bound(self, sid=None):
        return self.common.load(str(self.work), sid or self.sid)

    def record(self):
        return next(Path(store_root()).glob("*.json"))

    def attach(self, text="", name="w1"):
        result = subprocess.run(
            [*PY, "-c", ATTACH, json.dumps(self.config), name, text],
            env=child_env(),
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=WAIT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def headless(self, *, resume=False):
        handle = self.driver.start(
            "codex",
            "headless",
            "headless",
            dataclasses.replace(self.spec, sid=self.sid),
            options=StartOptions(resume=self.sid if resume else None),
        )
        self.addCleanup(handle.stop)
        stream = Stream(self, handle)
        return stream.until(Exited)
