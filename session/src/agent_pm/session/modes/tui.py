"""The tui Mode: a Tui's command in a tmux session; events from the hook records tui_hook.py
appends to its session dir, and from its pane.
"""

import contextlib
import fcntl
import json
import os
import signal
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Self

import agent_pm.session.engine.types
import agent_pm.utils
from agent_pm.session.adapters.base import Adapter, Tui
from agent_pm.session.adapters.registry import Registry
from agent_pm.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    DriverError,
    HookError,
    HookKind,
    Liveness,
    Mode,
    Paste,
    Prepared,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
    TuiSettings,
    Word,
)
from agent_pm.session.hosts.base import NAME
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.tmux_host import PaneStatus, TuiError
from agent_pm.session.modes import tui_hook
from agent_pm.session.modes.base import RunningMode, identity_drop
from agent_pm.utils import STATE, Display, json_records, module_roots, read_regular

TUI_DIR = f"{STATE}/tui"
WORDS = {
    HookKind.TURN_END: Word.DONE,
    HookKind.BLOCKED: Word.BLOCKED,
    HookKind.ACTIVITY: Word.WORKING,
}
SLICE = 0.1


def checked_name(name) -> str:
    if not isinstance(name, str) or not tmux_host.NAME.fullmatch(name):
        raise ConfigError(f"name must match [A-Za-z0-9_-]+, got {name!r:.80}")
    return name


def _events(events: str | None, *, host=tmux_host) -> str | None:
    if events is None:
        return None
    try:
        return host.validate_events(events)
    except TuiError as e:
        raise ConfigError(str(e)) from None


def _exit(status) -> int | None:
    return -signal.SIGHUP if status is None else status if isinstance(status, int) else None


def _state(prepared: Prepared, run: Tui, tmux: str) -> dict:
    spec = prepared.spec
    return {
        "runtime": spec.runtime,
        "workdir": spec.workdir,
        **prepared.info.persisted(),
        "events": spec.events,
        "words": {label: WORDS[kind] for label, kind in run.hooks.items() if kind in WORDS},
        "tmux_path": tmux,
        **run.hook_state(),
    }


def _bad(state: Mapping) -> str | None:
    """The first session.json field its readers refuse."""
    for field, check in tui_hook.SESSION_STATE.items():
        if field not in state or not check(state[field]):
            return field
    runtime = state.get("runtime")
    if not isinstance(runtime, str) or not NAME.match(runtime):
        return "runtime"
    try:
        SessionInfo.from_fields(state, strict=True)
    except ValueError as e:
        return str(e)
    return None


def _translated(
    run: Tui, records: Iterable[tuple[str, dict]]
) -> list[SessionEvent | Stderr | ClientBound]:
    return [event for label, payload in records for event in run.hook_events(label, payload)]


class _Store:
    """Tui session `name`'s dir and lock under TUI_DIR; what changes them takes the lock."""

    def __init__(self, name: str, *, host=tmux_host) -> None:
        self.name, self.host = checked_name(name), host
        self.root = os.path.expanduser(TUI_DIR)
        self.dir = os.path.join(self.root, name)
        self.state_file = os.path.join(self.dir, tui_hook.STATE)

    def hook_argv(self, source_roots: tuple[str, ...]) -> tuple[str, ...]:
        return (
            sys.executable,
            "-I",
            os.path.abspath(tui_hook.__file__),
            self.dir,
            json.dumps(source_roots),
        )

    def read(self) -> dict:
        try:
            state = tui_hook.read_state(self.state_file, error=HookError)
        except HookError as e:
            raise DriverError(f"no tui session {self.name}: {e}") from None
        if field := _bad(state):
            raise DriverError(f"no tui session {self.name}: {self.state_file}: bad {field}")
        return state

    def records(self, offset: int) -> tuple[list[tuple[str, dict]], int]:
        """hooks.jsonl's whole records from byte `offset`; the offset past them."""
        data = read_regular(os.path.join(self.dir, tui_hook.HOOKS), offset=offset) or b""
        end = data.rfind(b"\n") + 1
        found = []
        for record in json_records(data[:end].split(b"\n")):
            label, payload = record.get("label"), record.get("payload")
            if isinstance(label, str) and isinstance(payload, dict):
                found.append((label, payload))
        return found, offset + end

    def holder(self) -> str | None:
        """The pane under the name (None: absent, UNKNOWN: tmux failed)."""
        try:
            return self.host.pane(self.name)
        except TuiError:
            return PaneStatus.UNKNOWN

    def start(
        self, state: dict, commit: Callable[[], None], launch: Callable[[], None]
    ) -> str | None:
        """Under the lock: a fresh dir, commit(), launch() (a TuiError removes the dir), its
        pane (failing: closed).
        """
        if field := _bad(state):
            raise DriverError(f"tui session {self.name}: bad {field} {state.get(field)!r:.80}")
        with self._locked():
            made = self._make(state)
            try:
                commit()
            except ConfigError:
                self._unmake(made)
                raise
            try:
                launch()
            except TuiError as e:
                self._unmake(made)
                raise DriverError(f"cannot start tui session {self.name}: {e}") from e
            try:
                return self.host.pane(self.name)
            except TuiError as e:
                self._close()
                raise DriverError(f"cannot start tui session {self.name}: {e}") from e

    def close(self) -> None:
        with self._locked():
            self._close()

    def stop(self, pane: str | None) -> None:
        """Under the lock, unless another pane holds the name: kills `pane`'s session (the name
        absent: none; tmux failing: the name's), removes the dir.
        """
        with self._locked():
            holder = self.holder()
            if holder is None:
                self._drop()
            elif holder == PaneStatus.UNKNOWN:
                self._close()
            elif holder == pane:
                self._close(pane)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._root()
        path = os.path.join(self.root, f"{self.name}.lock")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError as e:
            raise DriverError(f"{path}: {e.strerror or e}") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _root(self) -> None:
        try:
            os.makedirs(self.root, 0o700, exist_ok=True)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    raise DriverError(f"{self.root}: not your directory")
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
        except OSError as e:
            raise DriverError(f"{self.root}: {e.strerror or e}") from None

    def _make(self, state: dict) -> os.stat_result:
        """A fresh dir with session.json, unless the session runs or is unknown; the dir's stat."""
        status = self.host.status(self.name)
        if status == PaneStatus.RUNNING:
            raise DriverError(
                f"tui session {self.name} is running; {self.host.attach_command(self.name)}"
            )
        if status == PaneStatus.UNKNOWN:
            raise DriverError(f"tui session {self.name}: its tmux state is unknown")
        folder = self.dir
        try:
            if os.path.lexists(folder):
                if not stat.S_ISDIR(os.lstat(folder).st_mode):
                    raise DriverError(f"{folder}: not a directory")
                self._remove()
            os.mkdir(folder, 0o700)
            made = os.lstat(folder)
        except FileExistsError:
            raise DriverError(f"tui session {self.name} is starting") from None
        except OSError as e:
            raise DriverError(f"{folder}: {e.strerror or e}") from None
        try:
            fd, temp = tempfile.mkstemp(dir=folder, prefix=".session.")
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(temp, self.state_file)
        except OSError as e:
            self._unmake(made)
            raise DriverError(f"{folder}: {e.strerror or e}") from None
        return made

    def _close(self, pane: str | None = None) -> None:
        try:
            self.host.kill(pane or self.name)
        except TuiError as e:
            if self.host.status(pane or self.name) is not None:
                raise DriverError(f"cannot close tui session {self.name}: {e}") from None
        self._drop()

    def _drop(self) -> None:
        try:
            self._remove()
        except OSError as e:
            raise DriverError(f"cannot close tui session {self.name}: {e.strerror or e}") from None

    def _remove(self, made: os.stat_result | None = None) -> None:
        try:
            st = os.lstat(self.dir)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode) and (made is None or os.path.samestat(st, made)):
            # A hook may outlive its pane; retire its public path before deleting files.
            with tempfile.TemporaryDirectory(dir=self.root, prefix=".closed-") as retired:
                os.rename(self.dir, retired)

    def _unmake(self, made: os.stat_result) -> None:
        with contextlib.suppress(OSError):
            self._remove(made)


@dataclass(frozen=True)
class _Start:
    prepared: Prepared
    run: Tui
    store: _Store
    settings: TuiSettings
    tmux: str

    @property
    def tags(self) -> dict[str, str]:
        return {"@sid": self.prepared.info.sid, "@runtime": self.prepared.spec.runtime or ""}

    def validate(self, display: Display) -> None:
        tags = {**self.tags, **({} if display is None else {"@display": display})}
        self.store.host.validate_start(self.store.name, self.prepared.argv, self.prepared.env, tags)


class TuiMode(RunningMode):
    """One tui session acting on its session's pane by id."""

    kind = Mode.TUI

    def __init__(
        self,
        prepared: Prepared,
        run: Tui,
        pane: str | None,
        store: _Store,
        *,
        offset: int = 0,
        replayed: tuple[SessionEvent | Stderr | ClientBound, ...] = (),
    ) -> None:
        """pane: the one it took; offset: its hook log's next record."""
        super().__init__(prepared)
        self.name, self.run, self.host, self._pane = store.name, run, store.host, pane
        self._store, self._replayed = store, replayed
        self._offset, self._dialogs, self._limit, self._code = offset, frozenset(), None, None
        self._live, self._gone = True, False

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        host=tmux_host,
        display: Display = None,
    ) -> Prepared:
        pending = cls._prepare(spec, adapter, registry=registry, host=host)
        try:
            pending.validate(display)
        except TuiError as error:
            raise ConfigError(str(error)) from error
        return pending.prepared

    @classmethod
    def _prepare(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        host=tmux_host,
    ) -> _Start:
        run = adapter.tui()
        if run is None:
            raise cls._unsupported(spec)
        settings = registry.tui()
        if spec.sid is None:
            raise ConfigError("sid: missing")
        name = checked_name(f"{spec.runtime}-{spec.sid[:8]}" if spec.name is None else spec.name)
        events = _events(spec.events, host=host)
        tmux = host.tmux_path()
        if tmux is None:
            raise DriverError("tmux not found")
        cls._selected(spec, adapter)
        drop = identity_drop(registry)
        store = _Store(name, host=host)
        hooks = (tmux, *([events] if events else []), store.root)
        spec = replace(spec, name=name, events=events, protect=(*spec.protect, *hooks))
        cls.check_protection(spec, adapter)
        spec = cls._noted(spec, adapter)
        roots = module_roots(agent_pm.session, agent_pm.utils)
        hook = store.hook_argv(roots)
        prepared = cls._launched(spec, lambda s: run.launch(s, hook, source_roots=roots), drop)
        state = _state(prepared, run, tmux)
        if field := _bad(state):
            raise DriverError(f"tui session {name}: bad {field} {state.get(field)!r:.80}")
        return _Start(prepared, run, store, settings, tmux)

    @classmethod
    def start(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        record: Callable[[SessionInfo], None],
        host=tmux_host,
        display: Display = None,
    ) -> Self:
        """tmux session spec.name, else <runtime>-<sid[:8]>, refused when running; spec.events
        gets done, blocked and dead lines; spec.protect gains the hook paths; display: its tmux
        option @display.
        """
        pending = cls._prepare(spec, adapter, registry=registry, host=host)
        prepared, run, store, settings, tmux = (
            pending.prepared,
            pending.run,
            pending.store,
            pending.settings,
            pending.tmux,
        )
        started = prepared.spec
        name = store.name
        tags = {**pending.tags, **({} if display is None else {"@display": display})}
        try:
            pending.validate(display)
            if started.events is not None:
                host.events_file(started.events)
        except TuiError as error:
            raise DriverError(f"cannot start tui session {name}: {error}") from error

        def launch() -> None:
            host.start(
                name,
                list(prepared.argv),
                events=started.events,
                status_line=settings.status_line,
                tags=tags,
                cwd=prepared.info.cwd,
                env=dict(prepared.env),
            )

        def commit() -> None:
            run.commit()
            record(prepared.info)

        pane = store.start(_state(prepared, run, tmux), commit, launch)
        return cls(prepared, run, pane, store)

    @classmethod
    def snapshot(cls, name: str) -> dict:
        """Tui session `name`'s session.json as start wrote it; else DriverError."""
        return _Store(name).read()

    @classmethod
    def attach(cls, name: str, state: Mapping, adapter: Adapter, *, host=tmux_host) -> Self:
        """Tui session `name` from its snapshot() if its pane is tagged with its sid; a fresh Tui
        restored, replays its hook log; events from the next record.
        """
        store = _Store(name, host=host)
        info = SessionInfo.from_fields(state)
        if info is None:
            raise DriverError(f"no tui session {name}: {store.state_file}: bad {_bad(state)}")
        try:
            run = adapter.tui()
        except ConfigError as e:
            raise DriverError(f"no tui session {name}: {e}") from None
        if run is None:
            raise DriverError(
                f"no tui session {name}: runtime {state['runtime']!r} has no tui mode"
            )
        try:
            pane = host.pane(name)
            tagged = None if pane is None else host.option(pane, "@sid")
        except TuiError:
            tagged = pane = None
        if tagged != info.sid:
            raise DriverError(f"no tui session {name}")
        spec = Spec(
            workdir=state["workdir"],
            runtime=state["runtime"],
            mode=Mode.TUI,
            sid=info.sid,
            name=name,
            events=state["events"],
        )
        try:
            run.restore(spec, state.get("adapter_state", {}))
        except ConfigError as e:
            raise DriverError(f"no tui session {name}: {e}") from None
        records, offset = store.records(0)
        replayed = _translated(run, records)
        return cls(
            Prepared(spec, info),
            run,
            pane,
            store,
            offset=offset,
            replayed=(*replayed,),
        )

    @classmethod
    def close(cls, name: str, *, host=tmux_host) -> None:
        """Kills tui session `name` (absent is fine), removes its dir; under its lock."""
        _Store(name, host=host).close()

    @property
    def replayed(self) -> tuple[SessionEvent | Stderr | ClientBound, ...]:
        """Attached: its hook log's events so far."""
        return self._replayed

    def marked_blocked(self) -> bool:
        """Its pane's @state is blocked (a dialog it showed); tmux failing: False."""
        try:
            return self.host.option(self._pane, "@state") == Word.BLOCKED
        except TuiError:
            return False

    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        """New records' events, else a pane scan, else a wait of `timeout` s; ended at once after
        stop() or once another pane holds the name.
        """
        found = self._read()
        if not found and not self._gone:
            status = self.host.status(self.name)
            try:
                screen = (
                    self.host.read(self._pane)
                    if status == PaneStatus.RUNNING and self._pane
                    else None
                )
            except TuiError:
                screen = None
            if (code := _exit(status := self._own(status))) is not None:
                self._code = code
                return self._read(), True
            if status == PaneStatus.RUNNING and screen is not None:
                found = self._scan(screen)
        deadline = time.monotonic() + timeout
        while not found and not self._gone and (left := deadline - time.monotonic()) > 0:
            time.sleep(min(SLICE, left))
            found = self._read()
        return found, self._gone

    def _holder(self) -> str | None:
        """Another pane under the name ends it."""
        holder = self._store.holder()
        if holder not in (self._pane, None, PaneStatus.UNKNOWN):
            self._end()
        return holder

    def _own(self, status):
        """None unless its own pane holds the name."""
        if status is None or status == PaneStatus.UNKNOWN:
            return status
        holder = self._holder()
        return (
            holder
            if holder == PaneStatus.UNKNOWN
            else status
            if holder and holder == self._pane
            else None
        )

    def _end(self) -> None:
        self._gone = True
        self._code = -signal.SIGHUP if self._code is None else self._code

    def _read(self) -> list[SessionEvent | Stderr | ClientBound]:
        if self._gone:
            return []
        records, end = self._store.records(self._offset)
        if end > self._offset:
            self._holder()  # lines under another pane are its session's
        if self._gone:
            return []
        self._offset = end
        return _translated(self.run, records)

    def _scan(self, screen: str) -> list[SessionEvent | Stderr | ClientBound]:
        """Since the last scan: Activity("") once a dialog went, Blocked per new dialog, LimitHit
        per appearance.
        """
        screen = " ".join(screen.split())
        shown = frozenset(d for d in self.run.dialogs if " ".join(d.split()) in screen)
        found: list[SessionEvent | Stderr | ClientBound] = (
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
            if self.host.option(self._pane, "@state") != Word.BLOCKED:
                self.host.mark(Word.BLOCKED, self.prepared.spec.events, pane=self._pane)

    def returncode(self, timeout: float) -> int | None:
        """The dead pane's status, -SIGHUP once gone; None while it runs after up to `timeout` s."""
        deadline = time.monotonic() + timeout
        while self._code is None:
            self._code = _exit(self._own(self.host.status(self.name)))
            if self._code is None:
                if (left := deadline - time.monotonic()) <= 0:
                    return None
                time.sleep(min(SLICE, left))
        return self._code

    def state(self) -> Liveness:
        status = self._own(self.host.status(self.name))
        if status is None:
            return Liveness.ABSENT
        return Liveness.UNKNOWN if status == PaneStatus.UNKNOWN else Liveness.LIVE

    def send(self, text: str) -> None:
        self._running()
        last = None
        try:
            for item in self.run.submit(text):
                if isinstance(item, Paste):
                    self.host.paste(self._pane, item.text)
                else:
                    if isinstance(last, Paste):
                        time.sleep(tmux_host.PAUSE)
                    self.host.keys(self._pane, item.name)
                last = item
        except TuiError as e:
            raise DriverError(f"send to tui session {self.name}: {e}") from e

    def screen(self, lines: int | None = None) -> str:
        self._running()
        try:
            return self.host.read(self._pane, lines)
        except TuiError as e:
            raise DriverError(f"screen of tui session {self.name}: {e}") from e

    def _running(self) -> None:
        if self._own(self.host.status(self.name)) != PaneStatus.RUNNING:
            raise DriverError(f"tui session {self.name} is not running")

    def stop(self) -> None:
        """Once; then ends (returncode: the code seen, else -SIGHUP)."""
        if self._live:
            self._store.stop(self._pane)
        self._live = False
        self._end()
