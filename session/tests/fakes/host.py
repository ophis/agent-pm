"""FakeHost, a Host that cannot display: real processes, each under supervisor.py in its own process group, state in
one dir per session under settings["root"] (default ~/.agent-pm/fake-host): meta.json, env.json, pid, rc, output,
and a terminal's FIFO input. Every call reads that state, so any process sees the sessions another started."""

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

from agent_pm.session import DriverError, Host, HostProcess, Terminal
from agent_pm.session.engine.api import Key, Paste
from agent_pm.utils.record import Display, Record

NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
KEYS = {"Enter": "\n", "Escape": "\x1b", "Tab": "\t"}
SUPERVISOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supervisor.py")
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(SUPERVISOR))))
PYTHONPATH = os.pathsep.join(os.path.join(_REPO, p, "src") for p in ("session", "utils"))
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
    def __init__(self, name: str, folder: str) -> None:
        self.name = name
        self.folder = folder

    def _pid(self) -> int | None:
        pid = _read(self.folder, "pid")
        return int(pid) if pid else None

    def alive(self) -> bool:
        pid = self._pid()
        if pid is None or _read(self.folder, "rc") is not None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        """The whole process group, whatever its leader's state: a child the program left is in it."""
        if (pid := self._pid()) is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        self.wait_exit(5)

    def close(self) -> None:
        self.kill()
        shutil.rmtree(self.folder, ignore_errors=True)

    def record(self) -> Record | None:
        line = _meta(self.folder).get("record")
        return None if line is None else Record.parse(line)

    def display(self) -> Display:
        return _meta(self.folder).get("display")


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
        return self._start("detach", name, argv, cwd, env, display, events, record)

    def terminal(self, name, argv, cwd, env, *, display=None, events=None, record=None):
        return self._start("terminal", name, argv, cwd, env, display, events, record)

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

    def _start(self, kind, name, argv, cwd, env, display, events, record):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f"name: want [A-Za-z0-9_-]+, not {name!r}")
        if not (isinstance(argv, list | tuple) and argv and all(isinstance(a, str) for a in argv)):
            raise ValueError(f"argv: want a non-empty list of str, not {argv!r}")
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            raise ValueError(f"cwd: want a directory, not {cwd!r}")
        folder = os.path.join(self.root(), name)
        if FakeProcess(name, folder).alive():
            raise DriverError(f"{self.name} session {name} is live")
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, mode=0o700)
        meta = {"kind": kind, "argv": list(argv), "cwd": cwd, "display": display}
        meta["record"] = None if record is None else record.to_line()
        for file, data in (("meta.json", meta), ("env.json", dict(env))):
            with open(os.path.join(folder, file), "w", encoding="utf-8") as f:
                json.dump(data, f)
        events = os.path.abspath(events) if events else ""
        own = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        with open(os.path.join(folder, "output"), "ab") as output:
            supervisor = subprocess.Popen(
                [sys.executable, "-P", SUPERVISOR, folder, events],
                env={**own, "PYTHONPATH": PYTHONPATH},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=output,
                start_new_session=True,
            )
        threading.Thread(target=supervisor.wait, daemon=True).start()
        deadline = time.monotonic() + START_TIMEOUT
        while _read(folder, "pid") is None:
            if supervisor.returncode is not None or time.monotonic() > deadline:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(supervisor.pid, signal.SIGKILL)
                tail = (_read(folder, "output") or "").strip().rpartition("\n")[2]
                raise DriverError(f"{self.name} session {name} did not start: {tail}")
            time.sleep(POLL)
        return KINDS[kind](name, folder)
