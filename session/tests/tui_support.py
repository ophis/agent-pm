"""Shared test fixtures and support."""

import json
import os
import queue
import shutil
import tempfile
import threading
import time
from unittest import mock

from agent_pm.session import Driver, Session
from agent_pm.session.engine.types import (
    Exited,
    IdleTimeout,
    Spec,
)
from agent_pm.session.hosts.tmux import transport
from agent_pm.session.hosts.tmux.host import Tmux, events_file, validate_events, validate_start

from session.tests import fake_adapter, live_tmux
from session.tests.case import Case, settings
from session.tests.modules import adapter_loader
from session.tests.threads import capture_errors

MANAGER = "mgr"

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"

IDLE = 0.5  # idle_timeout, in seconds

WAIT = 20  # upper bound on any wait, in seconds

DIALOG = "Pick one: 1. this 2. that"

TMUX = "/opt/tmux/bin/tmux"


def _validate_start(name, argv, env, tags):
    which = shutil.which

    def resolved(command, *, path=None):
        return command if command == "/bin/run" else which(command, path=path)

    with mock.patch("shutil.which", side_effect=resolved):
        validate_start(name, argv, env, tags)


def fake_host() -> mock.Mock:
    host = mock.create_autospec(Tmux, instance=True)
    host.PAUSE = Tmux.PAUSE
    host.attach_command.side_effect = transport.attach_command
    host.events_file.side_effect = events_file
    host.validate_events.side_effect = validate_events
    host.validate_start.side_effect = _validate_start
    host.tmux_path.return_value = TMUX
    host.status.return_value = None
    host.process_status.side_effect = lambda target: (host.process(target), host.status(target))
    host.read.return_value = ""
    host.option.return_value = None
    return host


class Stream:
    """A Handle's events drained by a daemon; its scoped exception hook carries failures."""

    def __init__(self, case, handle: Session):
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._drain, args=(handle,), daemon=True)
        case.enterContext(capture_errors(self.thread, self.queue.put))
        case.addCleanup(self.close, handle)
        self.thread.start()

    def _drain(self, handle):
        for event in handle.events():
            self.queue.put(event)

    def close(self, handle):
        handle.stop()
        self.thread.join(WAIT)
        if self.thread.is_alive():
            raise AssertionError(f"events() did not finish within {WAIT}s")

    def next(self, poke=None, *, idle=True):
        """The next event within WAIT s; idle=False skips IdleTimeout, poke runs between checks."""
        deadline = time.monotonic() + WAIT
        while True:
            try:
                item = self.queue.get(timeout=live_tmux.POLL)
            except queue.Empty:
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
                    "implementation": "agent_pm.session.adapters.fake:FakeAdapter",
                    "tui": True,
                    "scenario": os.path.join(self.dir, "scenario.json"),
                    "log": os.path.join(self.dir, "runtime.log"),
                }
            }
        )
        self.driver = self.driven()

    def driven(self, **dependencies) -> Driver:
        """A Driver of self.config loading the fake runtime, with `dependencies`."""
        return Driver(
            **self.config,
            class_loader=adapter_loader({"agent_pm.session.adapters.fake": fake_adapter}),
            **dependencies,
        )

    def start(self, name: str, *steps, sid=None, **kwargs) -> Session:
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
