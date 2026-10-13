"""Shared test fixtures and support."""

import json
import os
import queue
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from agent_pm.session import Driver
from agent_pm.session.engine import api
from agent_pm.session.engine.types import (
    Exited,
    IdleTimeout,
    Spec,
)

from session.tests import fake_adapter, live_tmux
from session.tests.case import Case, settings
from session.tests.modules import adapter_loader

MANAGER = "mgr"

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"

IDLE = 0.5  # idle_timeout, in seconds

WAIT = 20  # upper bound on any wait, in seconds

DIALOG = "Pick one: 1. this 2. that"


class Stream:
    """A Handle's events drained by a managed worker; its Future carries failures."""

    def __init__(self, case, handle: api.Handle):
        self.queue = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.result = self.executor.submit(self._drain, handle)
        case.addCleanup(self.close, handle)

    def _drain(self, handle):
        for event in handle.events():
            self.queue.put(event)

    def close(self, handle):
        handle.stop()
        try:
            self.result.result(timeout=WAIT)
        except TimeoutError:
            if self.result.done():
                raise
            raise AssertionError(f"events() did not finish within {WAIT}s") from None
        finally:
            self.executor.shutdown(wait=False, cancel_futures=True)

    def next(self, poke=None, *, idle=True):
        """The next event within WAIT s; idle=False skips IdleTimeout, poke runs between checks."""
        deadline = time.monotonic() + WAIT
        while True:
            try:
                item = self.queue.get(timeout=live_tmux.POLL)
            except queue.Empty:
                if self.result.done():
                    self.result.result()
                if time.monotonic() > deadline:
                    raise AssertionError(f"no event within {WAIT} s") from None
                if poke:
                    poke()
                continue
            if isinstance(item, BaseException):
                raise item
            if idle or not isinstance(item, IdleTimeout):
                return item

    def until_exited(self) -> list:
        """The events up to Exited, IdleTimeouts dropped."""
        found = [self.next(idle=False)]
        while not isinstance(found[-1], Exited):
            found.append(self.next(idle=False))
        return found

    def quiet(self, seconds: float) -> None:
        time.sleep(seconds)
        if not self.queue.empty():
            raise AssertionError(f"an event within {seconds} s: {self.queue.get()!r}")


class Live(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.events = os.path.join(self.dir, "events")
        self.config = settings(
            {
                "fake": {
                    "implementation": "adapters.fake:FakeAdapter",
                    "tui": True,
                    "scenario": os.path.join(self.dir, "scenario.json"),
                    "log": os.path.join(self.dir, "runtime.log"),
                }
            }
        )
        self.driver = Driver(
            **self.config,
            class_loader=adapter_loader({"agent_pm.session.adapters.fake": fake_adapter}),
        )

    def start(self, name: str, *steps, sid=None, **kwargs) -> api.Handle:
        """Tui session `name` of the fake runtime running `steps`, its events file self.events
        unless given.
        """
        with open(self.config["adapters"]["fake"]["scenario"], "w") as f:
            json.dump(list(steps), f)
        handle = self.driver.start(
            "fake",
            "tui",
            "hello",
            Spec(workdir=self.workdir, name=name, sid=sid),
            **{"events": self.events, **kwargs},
        )
        self.addCleanup(handle.stop)
        return handle

    def lines(self, *want: str, events: str | None = None) -> None:
        """Waits until the events file's (self.events unless given) lines, times dropped, are
        `want`; pokes the server meanwhile.
        """

        def check():
            self.poke()
            self.assertEqual(
                [line.split(" ", 1)[1] for line in live_tmux.events(events or self.events)],
                list(want),
            )
            return True

        live_tmux.wait(check, what=f"events lines {want}")

    def poke(self):
        # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so its status unset and
        # no pane-died, until another child of the server exits: a no-op job is one.
        self.server.tmux("run-shell", "-b", "true")
