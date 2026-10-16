"""PtyHost: each session a program on a pty that its own supervisor (supervisor.py) holds in a new
session, so it outlives its caller; its files in ~/.agent-pm/pty/<name>/ (state.py), its process
group in a ProcessControl. It cannot display. settings: outer_terminal, the variables of an
enclosing terminal that a terminal's program never gets; its TERM is TERM.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence

import agent_pm.utils
from agent_pm.session.engine.types import (
    ActivityState,
    ConfigError,
    HookCallback,
    Key,
    KeyPress,
    Launch,
    Liveness,
    Paste,
    SessionError,
    SessionRow,
    Word,
)
from agent_pm.session.hosts.base import Host, HostProcess, Terminal
from agent_pm.session.hosts.pty import hook, state
from agent_pm.session.hosts.pty.keys import encode
from agent_pm.utils import (
    CONFIG_NAME,
    Display,
    ProcessControl,
    Record,
    inode_lock,
    module_roots,
    sid_error,
    source_path,
)

NAME = re.compile(r"[A-Za-z0-9_-]+")
TERM = "xterm-256color"
SUPERVISOR = "agent_pm.session.hosts.pty.supervisor"
START_TIMEOUT = 10
KILL_WAIT = 5
PAUSE = 0.5  # before a key that follows a paste, as the program may still be taking the paste
POLL = 0.05
TIMEOUT = 10


class PtyProcess(HostProcess):
    """One launch: its generation (meta.json's id) and ProcessControl, kept after its name holds a
    later launch, which it never acts on.
    """

    def __init__(self, name: str, folder: str, *, meta: Mapping | None = None) -> None:
        self.name = name
        self.folder = folder
        found = state.meta(folder) if meta is None else meta
        self._id = found.get("id")
        path = found.get("control")
        self._control = ProcessControl(path) if isinstance(path, str) else None

    def _own(self) -> dict:
        found = state.meta(self.folder) if self._id is not None else {}
        return found if found.get("id") == self._id else {}

    @property
    def identity(self) -> str:
        return self._id if isinstance(self._id, str) else ""

    @property
    def token(self) -> str | None:
        return None if self._control is None else self._control.path

    def alive(self) -> bool:
        return (
            bool(self._own()) and state.read(self.folder, state.RC) is None and self._group_live()
        )

    def state(self) -> Liveness:
        return Liveness.LIVE if self._own() else Liveness.ABSENT

    def returncode(self) -> int | None:
        if not self._own():
            return -signal.SIGHUP
        value = state.read(self.folder, state.RC)
        if value is None:
            return None if self._supervised() else -signal.SIGHUP
        try:
            return int(value)
        except ValueError:
            return -signal.SIGHUP

    def activity(self) -> Word | None:
        value = state.read(self.folder, state.ACTIVITY) if self._own() else None
        return Word(value) if value in tuple(Word) else None

    def mark(self, word: Word, events: str | None) -> None:
        if self._own():
            state.mark(self.folder, word, events)

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if not (self.alive() or self._group_live()):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)

    def kill(self) -> None:
        self._signal(ProcessControl.kill)
        if not self.wait_exit(KILL_WAIT):
            raise SessionError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        """SIGTERM to its process group, SIGKILL after KILL_WAIT; then its dir goes."""
        if self._group_live():
            self._signal(ProcessControl.terminate)
            if not self.wait_exit(KILL_WAIT):
                self.kill()
        if self._own():
            shutil.rmtree(self.folder, ignore_errors=True)

    def record(self) -> Record | None:
        line = self._own().get("record")
        if isinstance(line, str):
            with contextlib.suppress(ValueError):
                return Record.parse(line)
        return None

    def publish(self, record: Record) -> None:
        if not isinstance(record, Record):
            raise ConfigError(f"record: want a Record, got {type(record).__name__}")
        found = self._own()
        if not found:
            raise SessionError(f"pty session {self.name} is gone")
        state.put(self.folder, state.META, json.dumps({**found, "record": record.to_line()}))

    def session_identity(self) -> tuple[str | None, str | None]:
        metadata = self._metadata()
        return metadata.get("sid"), metadata.get("generation")

    def display(self) -> Display:
        value = self._own().get("display")
        return value if isinstance(value, str) else None

    def row(self) -> SessionRow:
        metadata, receipt, activity = self._metadata(), self.record(), self.activity()
        sid = metadata.get("sid") or (None if receipt is None else receipt.sid)
        client = metadata.get("client") or (receipt.kind if receipt and receipt.sid else None)
        if not self.alive():
            now = ActivityState.DEAD
        elif activity in (Word.DONE, Word.BLOCKED):
            now = ActivityState(activity)
        else:
            now = ActivityState.WORKING
        return SessionRow(
            self.name,
            sid if isinstance(sid, str) and sid_error(sid) is None else None,
            client if isinstance(client, str) and CONFIG_NAME.fullmatch(client) else None,
            now,
            self.display(),
            None,
            None,
            own=False,
        )

    def _metadata(self) -> dict:
        found = self._own().get("metadata")
        return found if isinstance(found, dict) else {}

    def _supervised(self) -> bool:
        """Its supervisor may still write rc: running, or its pid not yet written."""
        pid = state.read(self.folder, state.PID)
        if pid is None:
            return True
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except (PermissionError, ValueError):
            pass
        return True

    def _group_live(self) -> bool:
        if self._control is None:
            return False
        try:
            return self._control.live()
        except (ConnectionRefusedError, ConnectionResetError):
            return False
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error

    def _signal(self, action) -> None:
        if self._control is None:
            return
        try:
            action(self._control)
        except (ConnectionRefusedError, ConnectionResetError):
            return
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error


class PtyTerminal(PtyProcess, Terminal):
    _pasted = False

    def send(self, action: Paste | Key) -> None:
        if isinstance(action, Paste):
            self._input({"paste": action.text})
            self._pasted = True
        else:
            self.press((KeyPress.parse(action.name),))

    def press(self, keys: Sequence[KeyPress]) -> None:
        data = b"".join(encode(key) for key in keys)
        if self._pasted:
            time.sleep(PAUSE)
        self._input({"keys": data.hex()})
        self._pasted = False

    def _input(self, request: dict) -> None:  # pragma: no cover
        if self._control is None or not self.alive():
            raise SessionError(f"tui session {self.name} is not running")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(TIMEOUT)
                connection.connect(os.path.join(self._control.path, state.SOCKET))
                connection.sendall(json.dumps(request).encode() + b"\n")
                with connection.makefile("rb") as stream:
                    reply = json.loads(stream.readline() or b"{}")
        except (OSError, ValueError) as error:
            raise SessionError(f"send to tui session {self.name}: {error}") from error
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            failure = reply.get("error") if isinstance(reply, dict) else reply
            raise SessionError(f"send to tui session {self.name}: {failure}")


KINDS = {"detach": PtyProcess, "terminal": PtyTerminal}


class PtyHost(Host):
    def __init__(self, name: str, settings: Mapping | None = None) -> None:
        super().__init__(name, settings)
        if extra := sorted(set(self.settings) - {"outer_terminal"}):
            raise ConfigError(f"unknown key {extra[0]!r} in [hosts.{name}]")
        outer = self.settings.get("outer_terminal", [])
        if not isinstance(outer, list | tuple) or not all(
            isinstance(key, str) and key for key in outer
        ):
            raise ConfigError(f"[hosts.{name}] outer_terminal: want a list of variable names")
        self.outer_terminal = frozenset(outer)

    def detach(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> PtyProcess:
        folder, meta = self._start(
            "detach",
            name,
            launch,
            env=launch.env,
            metadata={},
            events=events,
            record=record,
            display=display,
        )
        return PtyProcess(name, folder, meta=meta)

    def terminal(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> PtyTerminal:
        env = {k: v for k, v in launch.env.items() if k not in self.outer_terminal}
        env["TERM"] = TERM
        folder, meta = self._start(
            "terminal",
            name,
            launch,
            env=env,
            metadata=dict(metadata or {}),
            events=events,
            record=record,
            display=display,
        )
        return PtyTerminal(name, folder, meta=meta)

    def attach(self, name: str) -> PtyProcess:
        folder = os.path.join(state.root(), name)
        kind = state.meta(folder).get("kind") if NAME.fullmatch(name) else None
        kind = KINDS.get(kind) if isinstance(kind, str) else None
        if kind is None:
            raise SessionError(f"no {self.name} session {name}")
        return kind(name, folder)

    def list(self, display: Display = None) -> list[str]:
        root = state.root()
        names = sorted(os.listdir(root)) if os.path.isdir(root) and display != "" else []
        return [
            name
            for name in names
            if NAME.fullmatch(name)
            and (found := state.meta(os.path.join(root, name))).get("kind") in KINDS
            and (display is None or found.get("display") == display)
        ]

    def rows(self, display: Display = None) -> list[SessionRow]:
        found = []
        for name in self.list(display):
            with contextlib.suppress(SessionError):
                found.append(self.attach(name).row())
        return found

    def reclaim(self, token: str) -> HostProcess | None:
        """A handle on token's process group while it lives (wait_exit, kill); else None."""
        try:
            control = ProcessControl.attach(token)
        except (OSError, TypeError, ValueError):
            return None
        group = PtyProcess(os.path.basename(control.path), "", meta={"control": control.path})
        return None if group.wait_exit(0) else group

    def hook_callback(self, name: str, metadata: Mapping[str, str]) -> HookCallback:
        folder = os.path.join(state.root(), name)
        return HookCallback(hook.__name__, {"folder": folder, "generation": metadata["generation"]})

    def validate(self, name: str, launch: Launch, *, display: Display = None) -> None:
        super().validate(name, launch, display=display)
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ConfigError(f"name must match [A-Za-z0-9_-]+, got {name!r:.80}")

    def _start(self, kind, name, launch: Launch, **options) -> tuple[str, dict]:
        """options: display, events, record, metadata, env."""
        self.validate(name, launch, display=options["display"])
        options["events"] = self.prepare_events(self.events_path(options["events"]))
        os.makedirs(state.root(), mode=0o700, exist_ok=True)
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(inode_lock(os.path.join(state.root(), f"{name}.lock")))
            except OSError as error:
                raise SessionError(f"{kind} {name}: {error}") from error
            return self._create(kind, name, launch, **options)

    def _create(
        self, kind, name, launch: Launch, *, display, events, record, metadata, env
    ) -> tuple[str, dict]:
        """_start's work under the name's lock."""
        folder = os.path.join(state.root(), name)
        if os.path.isdir(folder):
            previous = PtyProcess(name, folder)
            if previous.alive():
                raise SessionError(f"{self.name} session {name} is live")
            previous.close()
            shutil.rmtree(folder, ignore_errors=True)
        os.mkdir(folder, mode=0o700)
        control = ProcessControl.prepare(
            launch.argv, launch.cwd, env=env, blocked_signals=launch.blocked_signals
        )
        meta = {
            "kind": kind,
            "id": uuid.uuid4().hex,
            "control": control.path,
            "display": display,
            "record": None if record is None else record.to_line(),
            "metadata": metadata,
        }
        supervisor = None
        try:
            state.put(folder, state.META, json.dumps(meta))
            os.symlink(
                os.path.join(control.path, state.SOCKET), os.path.join(folder, state.CONTROL)
            )
            supervisor = self._supervise(folder, events)
            deadline = time.monotonic() + START_TIMEOUT
            while state.read(folder, state.PID) is None:
                if supervisor.poll() is not None or time.monotonic() > deadline:
                    tail = (state.read(folder, state.OUTPUT) or "").strip().rpartition("\n")[2]
                    raise SessionError(f"{kind} {name}: did not start: {tail}")
                time.sleep(POLL)
            try:
                control.wait_started()
            except TimeoutError:
                raise
            except OSError as error:
                program = error.filename or launch.argv[0]
                raise SessionError(
                    f"{kind} {name}: cannot run {program}: {error.strerror}"
                ) from error
        except BaseException as error:
            with contextlib.suppress(OSError):
                control.cancel()
            if supervisor is not None and supervisor.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(supervisor.pid, signal.SIGKILL)
            shutil.rmtree(folder, ignore_errors=True)
            if isinstance(error, OSError):
                raise SessionError(f"{kind} {name}: {error}") from error
            raise
        return folder, meta

    def _supervise(self, folder: str, events: str | None) -> subprocess.Popen:
        roots = module_roots(sys.modules[__name__], agent_pm.utils)
        with open(os.path.join(folder, state.OUTPUT), "ab") as output:
            supervisor = subprocess.Popen(
                [
                    os.path.realpath(sys.executable),
                    "-P",
                    "-s",
                    "-m",
                    SUPERVISOR,
                    folder,
                    events or "",
                ],
                env={"PATH": os.defpath, **source_path(roots)},
                cwd="/",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=output,
                start_new_session=True,
            )
        threading.Thread(target=supervisor.wait, daemon=True).start()
        return supervisor
