"""The Driver's tui Mode: a Tui's command in a tmux session; events from the hook records tui_hook.py appends to its
session dir, and from its pane."""

import contextlib
import fcntl
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence

from adapters.base import (
    ACTIVITY,
    BLOCKED,
    TURN_END,
    Activity,
    Blocked,
    Event,
    Paste,
    SessionBound,
    SessionInfo,
    Stderr,
    Tui,
)
from utils.files import read_regular

from driver import tmux_host
from driver.errors import DriverError
from driver.modes import tui_hook

TUI_DIR = "~/.agent-pm/tui"
WORDS = {TURN_END: "done", BLOCKED: "blocked", ACTIVITY: "working"}
SLICE = 0.1


def session_dir(name: str) -> str:
    return os.path.join(os.path.expanduser(TUI_DIR), name)


def hook_argv(folder: str) -> tuple[str, ...]:
    return sys.executable, "-I", os.path.abspath(tui_hook.__file__), folder


def read_state(name: str) -> dict:
    """session.json via tui_hook.read_state; missing or invalid: DriverError."""
    try:
        return tui_hook.read_state(os.path.join(session_dir(name), tui_hook.STATE))
    except tui_hook.HookError as e:
        raise DriverError(f"no tui session {name}: {e}") from None


def close(name: str) -> None:
    """Kills the session (absent is fine), then removes its dir if a real directory; under the name's lock."""
    with lock(name):
        _close(name)


def _close(name: str) -> None:
    try:
        tmux_host.kill(name)
    except tmux_host.TuiError as e:
        if tmux_host.status(name) is not None:
            raise DriverError(f"cannot close tui session {name}: {e}") from None
    try:
        _remove(session_dir(name))
    except OSError as e:
        raise DriverError(f"cannot close tui session {name}: {e.strerror or e}") from None


@contextlib.contextmanager
def lock(name: str) -> Iterator[None]:
    _tui_dir()
    path = os.path.join(os.path.expanduser(TUI_DIR), f"{name}.lock")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as e:
        raise DriverError(f"{path}: {e.strerror or e}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _remove(path: str, made: os.stat_result | None = None) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode) and (made is None or os.path.samestat(st, made)):
        shutil.rmtree(path)


def _tui_dir() -> None:
    root = os.path.expanduser(TUI_DIR)
    try:
        os.makedirs(root, 0o700, exist_ok=True)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise DriverError(f"{root}: not your directory")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    except OSError as e:
        raise DriverError(f"{root}: {e.strerror or e}") from None


def _records(data: bytes) -> Iterator[tuple[str, dict]]:
    for line in data.split(b"\n"):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        label, payload = (
            (record.get("label"), record.get("payload"))
            if isinstance(record, dict)
            else (None, None)
        )
        if isinstance(label, str) and isinstance(payload, dict):
            yield label, payload


def _exit(status) -> int | None:
    return -signal.SIGHUP if status is None else status if isinstance(status, int) else None


class TuiMode:
    """One tui session, driver.api's Mode, bound to its session's pane."""

    def __init__(self, name: str, run: Tui, sid: str, events: str | None):
        self.name, self.run, self.sid, self.events = name, run, sid, events
        self.dir = session_dir(name)
        self.hooks = os.path.join(self.dir, tui_hook.HOOKS)
        self.bound, self.reply = "", None
        self._offset, self._dialogs, self._limit, self._code = 0, frozenset(), None, None
        self._made, self._live, self._pane, self._gone = None, False, None, False

    @classmethod
    def prepare(
        cls,
        name: str,
        run: Tui,
        info: SessionInfo,
        events: str | None,
        *,
        runtime: str,
        workdir: str,
    ) -> "TuiMode":
        """A fresh session dir with session.json, unless the session runs or is unknown."""
        state = {
            "runtime": runtime,
            "sid": info.sid,
            "workdir": workdir,
            "cwd": info.cwd,
            "project": info.project,
            "transcript": info.transcript,
            "resume": info.resume,
            "events": events,
            "words": {label: WORDS[kind] for label, kind in run.hooks.items() if kind in WORDS},
        }
        for field, check in tui_hook.SESSION_STATE.items():
            if not check(state[field]):
                raise DriverError(f"tui session {name}: bad {field} {state[field]!r:.80}")
        status = tmux_host.status(name)
        if status == tmux_host.RUNNING:
            raise DriverError(f"tui session {name} is running; {tmux_host.attach_command(name)}")
        if status == tmux_host.UNKNOWN:
            raise DriverError(f"tui session {name}: its tmux state is unknown")
        _tui_dir()
        mode = cls(name, run, info.sid, events)
        try:
            if os.path.lexists(mode.dir):
                if not stat.S_ISDIR(os.lstat(mode.dir).st_mode):
                    raise DriverError(f"{mode.dir}: not a directory")
                shutil.rmtree(mode.dir)
            os.mkdir(mode.dir, 0o700)
            mode._made = os.lstat(mode.dir)
        except FileExistsError:
            raise DriverError(f"tui session {name} is starting") from None
        except OSError as e:
            raise DriverError(f"{mode.dir}: {e.strerror or e}") from None
        try:
            fd, temp = tempfile.mkstemp(dir=mode.dir, prefix=".session.")
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(temp, os.path.join(mode.dir, tui_hook.STATE))
        except OSError as e:
            mode._unmake()
            raise DriverError(f"{mode.dir}: {e.strerror or e}") from None
        return mode

    def begin(
        self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], status_line: bool = False
    ) -> None:
        """Starts argv tagged with the sid, then takes its pane; failing, ends it and removes the dir prepare made."""
        try:
            tmux_host.start(
                self.name,
                list(argv),
                cwd=cwd,
                env=dict(env),
                events=self.events,
                status_line=status_line,
                tags={"@sid": self.sid},
            )
        except tmux_host.TuiError as e:
            self._unmake()
            raise DriverError(f"cannot start tui session {self.name}: {e}") from e
        try:
            self._pane = tmux_host.pane(self.name)
        except tmux_host.TuiError as e:
            _close(self.name)  # start() holds the name's lock
            raise DriverError(f"cannot start tui session {self.name}: {e}") from e
        self._live = True

    def _unmake(self) -> None:
        with contextlib.suppress(OSError):
            _remove(self.dir, self._made)

    @classmethod
    def attach(cls, name: str, state: dict, run: Tui) -> "TuiMode":
        """read_state's session if tagged with its sid; reply: its last TURN_END's; events from its next record."""
        try:
            tagged, pane = tmux_host.option(name, "@sid"), tmux_host.pane(name)
        except tmux_host.TuiError:
            tagged = pane = None
        if tagged != state["sid"]:
            raise DriverError(f"no tui session {name}")
        mode = cls(name, run, state["sid"], state["events"])
        mode._pane = pane
        data = read_regular(mode.hooks) or b""
        mode._offset = data.rfind(b"\n") + 1
        turn = None
        for label, payload in _records(data[: mode._offset]):
            mode.bound = run.session_id(payload) or mode.bound
            if run.hooks.get(label) == TURN_END:
                turn = payload
        mode.reply = None if turn is None else run.reply(turn)
        mode._live = True
        return mode

    def poll(self, timeout: float) -> tuple[list[Event | Stderr], bool]:
        """New records' events, else a pane scan, else a wait of `timeout` s; ended at once after stop() or once another
        pane holds the name."""
        found = self._read()
        if not found and not self._gone:
            status = tmux_host.status(self.name)
            try:  # before _own: a screen read before its pane is confirmed is its own
                screen = tmux_host.read(self.name) if status == tmux_host.RUNNING else None
            except tmux_host.TuiError:
                screen = None
            if (code := _exit(status := self._own(status))) is not None:
                self._code = code
                return self._read(), True
            if status == tmux_host.RUNNING and screen is not None:
                found = self._scan(screen)
        deadline = time.monotonic() + timeout
        while not found and not self._gone and (left := deadline - time.monotonic()) > 0:
            time.sleep(min(SLICE, left))
            found = self._read()
        return found, self._gone

    def _holder(self) -> str | None:
        """The pane under the name (None: absent, UNKNOWN: tmux failed)."""
        try:
            holder = tmux_host.pane(self.name)
        except tmux_host.TuiError:
            return tmux_host.UNKNOWN
        if holder not in (self._pane, None):
            self._end()
        return holder

    def _own(self, status):
        """None unless its own pane holds the name."""
        if status is None or status == tmux_host.UNKNOWN:
            return status
        holder = self._holder()
        return (
            holder
            if holder == tmux_host.UNKNOWN
            else status
            if holder and holder == self._pane
            else None
        )

    def _end(self) -> None:
        self._gone = True
        self._code = -signal.SIGHUP if self._code is None else self._code

    def _read(self) -> list[Event]:
        data = b"" if self._gone else read_regular(self.hooks, offset=self._offset) or b""
        end = data.rfind(b"\n") + 1
        if end:
            self._holder()  # lines under another pane are its session's
        if self._gone:
            return []
        self._offset += end
        found = []
        for label, payload in _records(data[:end]):
            for event in self.run.hook_events(label, payload, self.bound):
                if isinstance(event, SessionBound):
                    self.bound = event.native_id
                found.append(event)
        return found

    def _scan(self, screen: str) -> list[Event]:
        """Since the last scan: Activity("") once a dialog went, Blocked per new dialog, LimitHit per appearance."""
        screen = " ".join(screen.split())
        shown = frozenset(d for d in self.run.dialogs if " ".join(d.split()) in screen)
        found = (
            [Activity("")] if self._dialogs - shown else []
        )  # first: a later Blocked keeps the idle pause
        found += [Blocked(d) for d in self.run.dialogs if d in shown - self._dialogs]
        limit = self.run.limit(screen)
        if limit is not None and self._limit is None:
            found.append(limit)
        if shown - self._dialogs:
            self._mark_blocked()
        self._dialogs, self._limit = shown, limit
        return found

    def _mark_blocked(self) -> None:
        with contextlib.suppress(tmux_host.TuiError):
            if tmux_host.option(self.name, "@state") != "blocked":
                tmux_host.mark("blocked", self.events, pane=self._pane)

    def returncode(self, timeout: float) -> int | None:
        """The dead pane's status, -SIGHUP once gone; None while it runs after up to `timeout` s."""
        deadline = time.monotonic() + timeout
        while self._code is None:
            self._code = _exit(self._own(tmux_host.status(self.name)))
            if self._code is None:
                if (left := deadline - time.monotonic()) <= 0:
                    return None
                time.sleep(min(SLICE, left))
        return self._code

    def state(self) -> str:
        status = self._own(tmux_host.status(self.name))
        return "absent" if status is None else "unknown" if status == tmux_host.UNKNOWN else "live"

    def send(self, text: str) -> None:
        self._running()
        last = None
        try:
            for item in self.run.submit(text):
                if isinstance(item, Paste):
                    tmux_host.paste(self.name, item.text)
                else:
                    if isinstance(last, Paste):
                        time.sleep(tmux_host.PAUSE)
                    tmux_host.keys(self.name, item.name)
                last = item
        except tmux_host.TuiError as e:
            raise DriverError(f"send to tui session {self.name}: {e}") from e

    def screen(self, lines: int | None = None) -> str:
        self._running()
        try:
            return tmux_host.read(self.name, lines)
        except tmux_host.TuiError as e:
            raise DriverError(f"screen of tui session {self.name}: {e}") from e

    def _running(self) -> None:
        if self._own(tmux_host.status(self.name)) != tmux_host.RUNNING:
            raise DriverError(f"tui session {self.name} is not running")

    def stop(self) -> None:
        """Under the name's lock, close() unless another pane holds the name; then ends (returncode: the code seen,
        else -SIGHUP)."""
        if self._live:
            with lock(self.name):
                if self._holder() in (self._pane, None, tmux_host.UNKNOWN):
                    _close(self.name)
        self._live = False
        self._end()
