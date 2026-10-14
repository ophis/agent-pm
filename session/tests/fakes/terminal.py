"""Nonspawning Host and Terminal doubles with configurable boundary calls."""

import os
import signal
from unittest import mock

from agent_pm.session import Displayable, Host, SessionError, Terminal
from agent_pm.session.engine.types import ActivityState, Launch, Liveness, SessionRow


class MemoryTerminal(Terminal):
    def __init__(self, host, name, identity, receipt=None, *, metadata=None, display=None):
        self.host, self.name, self._identity = host, name, identity
        self.calls = mock.Mock()
        self.calls.alive.return_value = True
        self.calls.state.return_value = Liveness.LIVE
        self.calls.returncode.return_value = None
        self.calls.activity.return_value = None
        self.calls.record.return_value = receipt
        self.calls.row.return_value = None
        self.calls.session_identity.return_value = (
            (metadata or {}).get("sid"),
            (metadata or {}).get("generation"),
        )
        self.metadata = dict(metadata or {})
        self._display = display

    @property
    def identity(self):
        return self._identity

    def _own(self):
        return self.host.processes.get(self.name) is self

    def alive(self):
        return self._own() and self.calls.alive()

    def state(self):
        return self.calls.state() if self._own() else Liveness.ABSENT

    def returncode(self):
        return self.calls.returncode() if self._own() else -signal.SIGHUP

    def activity(self):
        return self.calls.activity() if self._own() else None

    def mark(self, word, events):
        if self._own():
            self.calls.mark(word, events)
            self.calls.activity.return_value = word

    def send(self, action):
        if not self.alive():
            raise SessionError(f"terminal {self.name} is not running")
        self.calls.send(action)

    def press(self, keys):
        keys = tuple(keys)
        if not self.alive():
            raise SessionError(f"terminal {self.name} is not running")
        self.calls.press(keys)

    def close(self):
        self.calls.close()
        if self._own():
            self.host.processes.pop(self.name)

    def kill(self):
        self.calls.kill()
        self.calls.alive.return_value = False

    def wait_exit(self, timeout):
        self.calls.wait_exit(timeout)
        return not self.alive()

    def record(self):
        return self.calls.record() if self._own() else None

    def session_identity(self):
        return self.calls.session_identity() if self._own() else (None, None)

    def display(self):
        receipt = self.record()
        return self._display if receipt is None else receipt.display

    def row(self):
        if (row := self.calls.row()) is not None:
            return row
        receipt = self.record()
        return SessionRow(
            self.name,
            self.metadata.get("sid") if receipt is None else receipt.sid,
            self.metadata.get("client") if receipt is None else receipt.kind,
            ActivityState.WORKING if self.alive() else ActivityState.DEAD,
            self.display(),
            None,
            None,
            own=receipt is not None or self.metadata.get("sid") is not None,
        )


class MemoryHost(Host):
    def __init__(self, name="memory", *, hook=None):
        super().__init__(name)
        self.calls = mock.Mock()
        self.processes = {}
        self.serial = 0
        self.hook = hook
        self.calls.list.return_value = None
        self.calls.display.return_value = None

    def create(self, name, sid=None, *, identity=None, receipt=None, metadata=None, display=None):
        self.serial += 1
        generation = identity or f"generation-{self.serial}"
        if metadata is None:
            metadata = {"sid": sid, "generation": generation}
        process = MemoryTerminal(
            self, name, generation, receipt, metadata=metadata, display=display
        )
        self.processes[name] = process
        return process

    def validate(self, name, launch: Launch, *, display=None):
        self.calls.validate(name, launch, display=display)
        super().validate(name, launch, display=display)

    def hook_callback(self, name, metadata):
        self.calls.hook_callback(name, metadata)
        return self.hook

    def display(self, value):
        self.calls.display(value)
        return value if self.calls.display.return_value is None else self.calls.display.return_value

    def terminal(
        self, name, launch: Launch, *, display=None, events=None, record=None, metadata=None
    ):
        self.calls.terminal(
            name, launch, display=display, events=events, record=record, metadata=metadata
        )
        if events:
            fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        return self.create(name, receipt=record, metadata=metadata, display=display)

    def detach(self, name, launch: Launch, *, display=None, events=None, record=None):
        self.calls.detach(name, launch, display=display, events=events, record=record)
        return self.create(name, receipt=record, display=display)

    def attach(self, name):
        self.calls.attach(name)
        if name not in self.processes:
            raise SessionError(f"no {self.name} session {name}")
        return self.processes[name]

    def list(self, display=None):
        listed = self.calls.list(display)
        if listed is not None:
            return listed
        return sorted(
            name
            for name, process in self.processes.items()
            if display is None or display != "" and process.display() == display
        )


class ScreenHost(MemoryHost, Displayable):
    def __init__(self, name="memory", **options):
        super().__init__(name, **options)
        self.calls.screen.return_value = ""

    def screen(self, name, lines=None):
        if isinstance(name, MemoryTerminal) and not name._own():
            raise SessionError(f"terminal {name.name} is not running")
        return self.calls.screen(name, lines)

    def show(self, name, *, display=None, command=None):
        return self.calls.show(name, display=display, command=command)

    def attach_command(self, name):
        return self.calls.attach_command(name)

    def layout(self, *, display=None):
        return self.calls.layout(display=display)

    def tile(self, window, *, wait=True, if_panes_changed=False):
        self.calls.tile(window, wait=wait, if_panes_changed=if_panes_changed)
