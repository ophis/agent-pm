"""FakeHost runs programs under a generation-scoped ProcessControl; supervisor.py writes session
state and events. Metadata lives under settings["root"] (default ~/.agent-pm/fake-host); a handle
keeps its launch's controller even when the session name is reused.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from agent_pm.session import DriverError, Host, HostProcess, Terminal
from agent_pm.session.engine.types import Key, Paste
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.utils import Display, ProcessControl, Record, import_roots

NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
KEYS = {"Enter": "\n", "Escape": "\x1b", "Tab": "\t"}
SUPERVISOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supervisor.py")
PYTHONPATH = os.pathsep.join(import_roots())
START_TIMEOUT = 10
POLL = 0.05


def _read(folder: str, file: str) -> str | None:
    try:
        with open(os.path.join(folder, file), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _meta(folder: str) -> dict:
    try:
        return json.loads(_read(folder, "meta.json") or "{}")
    except ValueError:
        return {}


class FakeProcess(HostProcess):
    def __init__(self, name: str, folder: str, *, meta: dict | None = None) -> None:
        self.name = name
        self.folder = folder
        meta = _meta(folder) if meta is None else meta
        self._id = meta.get("id")
        control = meta.get("control")
        self._control = ProcessControl(control) if control else None

    def _own(self) -> dict:
        meta = _meta(self.folder)
        return meta if self._id is not None and meta.get("id") == self._id else {}

    def alive(self) -> bool:
        return bool(self._own()) and _read(self.folder, "rc") is None and self._live_group()

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive() or self._live_group():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        if self._control is not None:
            try:
                self._control.kill()
            except OSError as error:
                raise DriverError(f"process group {self.name}: {error}") from error
        if not self.wait_exit(5):
            raise DriverError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        self.kill()
        if self._own():
            shutil.rmtree(self.folder, ignore_errors=True)

    def record(self) -> Record | None:
        line = self._own().get("record")
        return None if line is None else Record.parse(line)

    def display(self) -> Display:
        return self._own().get("display")

    def _live_group(self) -> bool:
        if self._control is None:
            return False
        try:
            return self._control.live()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error


class FakeTerminal(FakeProcess, Terminal):
    def send(self, action: Paste | Key) -> None:
        if isinstance(action, Paste):
            data = action.text.encode()
        elif isinstance(action, Key) and action.name in KEYS:
            data = KEYS[action.name].encode()
        else:
            raise DriverError(f"{self.name}: cannot send {action!r}")
        if not self.alive():
            raise DriverError(f"{self.name}: not alive")
        try:
            fd = os.open(os.path.join(self.folder, "input"), os.O_WRONLY | os.O_NONBLOCK)
            try:
                while data:
                    data = data[os.write(fd, data) :]
            finally:
                os.close(fd)
        except OSError as e:
            raise DriverError(f"{self.name}: cannot send: {e}") from None


KINDS = {"detach": FakeProcess, "terminal": FakeTerminal}


class FakeHost(Host):
    def root(self) -> str:
        return os.path.expanduser(self.settings.get("root", "~/.agent-pm/fake-host"))

    def detach(self, name, argv, cwd, env, *, display=None, events=None, record=None):
        return self._start(
            "detach", name, argv, cwd, env, display=display, events=events, record=record
        )

    def terminal(self, name, argv, cwd, env, *, display=None, events=None, record=None):
        return self._start(
            "terminal", name, argv, cwd, env, display=display, events=events, record=record
        )

    def attach(self, name: str) -> FakeProcess:
        folder = os.path.join(self.root(), name)
        cls = KINDS.get(_meta(folder).get("kind")) if NAME.fullmatch(name) else None
        if cls is None:
            raise DriverError(f"no {self.name} session {name}")
        return cls(name, folder)

    def list(self, display=None) -> list[str]:
        root = self.root()
        names = sorted(os.listdir(root)) if os.path.isdir(root) and display != "" else []
        return [
            name
            for name in names
            if NAME.fullmatch(name)
            and (meta := _meta(os.path.join(root, name))).get("kind") in KINDS
            and (display is None or meta.get("display") == display)
        ]

    def _start(self, kind, name, argv, cwd, env, *, display, events, record):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f"name: want [A-Za-z0-9_-]+, not {name!r}")
        if not (isinstance(argv, list | tuple) and argv and all(isinstance(a, str) for a in argv)):
            raise ValueError(f"argv: want a non-empty list of str, not {argv!r}")
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            raise ValueError(f"cwd: want a directory, not {cwd!r}")
        folder = os.path.join(self.root(), name)
        previous = FakeProcess(name, folder)
        if previous.alive():
            raise DriverError(f"{self.name} session {name} is live")
        if os.path.isdir(folder):
            previous.close()
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, mode=0o700)
        env = {key: value for key, value in env.items() if key not in tmux_host.TERMINAL_KEYS}
        env["PWD"] = os.path.abspath(cwd)
        control = ProcessControl.prepare(argv, cwd, env=env)
        meta = {
            "control": control.path,
            "kind": kind,
            "id": uuid.uuid4().hex,
            "argv": list(argv),
            "cwd": cwd,
            "display": display,
        }
        meta["record"] = None if record is None else record.to_line()
        for file, data in (("meta.json", meta), ("env.json", dict(env))):
            with open(os.path.join(folder, file), "w", encoding="utf-8") as f:
                json.dump(data, f)
        events = os.path.abspath(events) if events else ""
        own = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        with open(os.path.join(folder, "output"), "ab") as output:
            supervisor = subprocess.Popen(
                [sys.executable, "-P", "-S", SUPERVISOR, folder, events],
                env={**own, "PYTHONPATH": PYTHONPATH},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=output,
                start_new_session=True,
            )
        threading.Thread(target=supervisor.wait, daemon=True).start()
        deadline = time.monotonic() + START_TIMEOUT
        while _read(folder, "pid") is None and not os.path.isfile(
            os.path.join(control.path, "started.json")
        ):
            if supervisor.returncode is not None or time.monotonic() > deadline:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(supervisor.pid, signal.SIGKILL)
                tail = (_read(folder, "output") or "").strip().rpartition("\n")[2]
                raise DriverError(f"{self.name} session {name} did not start: {tail}")
            time.sleep(POLL)
        control.wait_started()
        return KINDS[kind](name, folder, meta=meta)
