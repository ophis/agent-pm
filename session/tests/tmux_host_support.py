"""Tmux Host fixtures: recorded tmux calls and controller responses, with owned state."""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_pm.session.hosts.tmux import TmuxHost, tmux_host
from agent_pm.utils import ProcessControl, Record

from session.tests.case import Case

LINE = Record(1, "role", "r1", ("r1",), None, None, ("ctl",), ("ctl", "resume"), None, None)
PROCESS = "4242 $3 %7 4343"


def fake_tmux() -> mock.Mock:
    """Recorded transport with real validation and command formatting."""
    tmux = mock.create_autospec(tmux_host)
    for name in ("PaneStatus", "TuiError", "Retile"):
        setattr(tmux, name, getattr(tmux_host, name))
    for name in ("attach_command", "events_file", "validate_events", "validate_start"):
        getattr(tmux, name).side_effect = getattr(tmux_host, name)
    tmux.status.return_value = tmux_host.PaneStatus.RUNNING
    tmux.listing.return_value = []
    tmux.opener.return_value = "mgr"
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
        self.tmux = fake_tmux()
        self.host = TmuxHost("tmux", {"workers_per_column": 2}, tmux=self.tmux)
        self.killpg = self.enterContext(
            mock.patch("os.killpg", side_effect=AssertionError("unsafe signal"))
        )
        self.control = self.controller()
        self.addCleanup(self.control.remove)
        self.completed(self.control.path)
        self.controlled = True
        self.kind = "detach"
        self.tmux.option.side_effect = lambda _pane, key: (
            self.control.path
            if key == "@control" and self.controlled
            else self.kind
            if key == "@kind"
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

        def private(**kw):
            self.assertEqual(kw, {"prefix": "apm-group-", "dir": "/tmp"})
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
        self.detached(control=kw["tags"]["@control"])

    def active(self, *responses):
        self.controlled = True
        Path(self.control.path, "done.json").unlink(missing_ok=True)
        stream = self.connection.makefile.return_value.__enter__.return_value
        stream.readline.side_effect = [
            json.dumps(response).encode() + b"\n" for response in responses
        ]
