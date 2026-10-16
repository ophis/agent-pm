"""Tmux Host fixtures: recorded tmux calls and controller responses, with owned state."""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_pm.session.hosts.tmux import TmuxHost, types
from agent_pm.session.hosts.tmux.transport import Tmux
from agent_pm.utils import ProcessControl, Record

from session.tests.case import Case
from session.tests.tmux_fixtures import FakeTmux, which

LINE = Record(
    1,
    "role",
    "r1",
    ("r1",),
    None,
    "/w",
    ("agent-pm-delegate",),
    ("agent-pm-delegate", "resume", "/w"),
    None,
    None,
)
PROCESS = "4242 $3 %7 4343"


def fake_tmux(runner: FakeTmux) -> mock.Mock:
    """A Tmux whose session verbs are recorded; its run, checked and proc (all that Grid, Placement
    and Iterm use) play runner.
    """
    tmux = mock.create_autospec(Tmux, instance=True)
    tmux.PAUSE = Tmux.PAUSE
    tmux.proc = runner
    tmux.run.side_effect = Tmux(runner).run
    tmux.checked.side_effect = Tmux(runner).checked
    tmux.status.return_value = types.PaneStatus.RUNNING
    tmux.process_status.side_effect = lambda target: (tmux.process(target), tmux.status(target))
    tmux.listing.return_value = []
    tmux.option.return_value = None
    tmux.detach.return_value = tmux.process.return_value = PROCESS
    return tmux


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.tool = os.path.join(self.dir, "tool")
        Path(self.tool).touch(mode=0o700)
        self.enterContext(which(self.tool))
        self.runner = FakeTmux()
        self.tmux = fake_tmux(self.runner)
        self.host = TmuxHost("tmux", {"workers_per_column": 2}, tmux=self.tmux)
        self.killpg = self.enterContext(
            mock.patch("os.killpg", side_effect=AssertionError("unsafe signal"))
        )
        self.control = self.controller()
        self.addCleanup(self.control.remove)
        self.completed(self.control.path)
        self.controlled = True
        self.kind = "detach"
        self.recorded = PROCESS
        self.tmux.option.side_effect = lambda _pane, key: (
            self.control.path
            if key == "@control" and self.controlled
            else self.kind
            if key == "@kind"
            else self.recorded
            if key == "@process"
            else self.tmux.option.return_value
        )
        self.tmux.detach.side_effect = self.detached
        self.tmux.start.side_effect = self.terminal_started
        self.socket = self.enterContext(mock.patch("socket.socket"))
        self.connection = self.socket.return_value.__enter__.return_value
        self.connection.makefile.return_value.__enter__.return_value.readline.return_value = (
            b'{"live": true}\n'
        )
        allocate = tempfile.mkdtemp

        def private(*args, **kw):
            if args:
                return allocate(*args, **kw)
            self.assertEqual(
                kw,
                {"prefix": "apm-group-", "dir": os.environ.get("AGENT_PM_CONTROL_ROOT") or "/tmp"},
            )
            return allocate(prefix=kw["prefix"], dir=self.dir)

        self.enterContext(mock.patch("tempfile.mkdtemp", side_effect=private))

    def controller(self):
        path = os.path.join(self.dir, "controller")
        os.mkdir(path, 0o700)
        return ProcessControl(path)

    @staticmethod
    def completed(path):
        for name, message in (("started.json", {"started": True}), ("done.json", {"rc": 0})):
            Path(path, name).write_text(json.dumps(message))

    def detached(self, *_args, **kw):
        self.control = ProcessControl(kw["control"])
        self.addCleanup(self.control.remove)
        Path(self.control.path, "started.json").write_text('{"started": true}')
        return PROCESS

    def terminal_started(self, *_args, **kw):
        self.kind = "terminal"
        return self.detached(control=kw["tags"]["@control"])

    def active(self, *responses):
        self.controlled = True
        Path(self.control.path, "done.json").unlink(missing_ok=True)
        stream = self.connection.makefile.return_value.__enter__.return_value
        stream.readline.side_effect = [
            json.dumps(response).encode() + b"\n" for response in responses
        ]
