"""The tui Mode: an Adapter command on a Host terminal; events from the hook records
appended to its session directory and from its terminal.
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
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Self

import agent_pm.session.engine.types
import agent_pm.utils
from agent_pm.session.adapters.base import Adapter, Tui
from agent_pm.session.adapters.registry import Registry
from agent_pm.session.engine.types import (
    TAG,
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    DriverError,
    HookError,
    HookKind,
    Launch,
    Liveness,
    Mode,
    Prepared,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
    Word,
)
from agent_pm.session.hosts.base import Displayable, Host, HostProcess, Terminal
from agent_pm.session.modes import tui_hook
from agent_pm.session.modes.base import RunningMode, identity_drop
from agent_pm.utils import CONFIG_NAME as NAME
from agent_pm.utils import STATE, Display, json_records, module_roots, read_regular

TUI_DIR = f"{STATE}/tui"
WORDS = {
    HookKind.TURN_END: Word.DONE,
    HookKind.TURN_FAILED: Word.DONE,
    HookKind.BLOCKED: Word.BLOCKED,
    HookKind.ACTIVITY: Word.WORKING,
}
SLICE = 0.1


def checked_name(name) -> str:
    if not isinstance(name, str) or not TAG.fullmatch(name):
        raise ConfigError(f"name must match [A-Za-z0-9_-]+, got {name!r:.80}")
    return name


def _state(prepared: Prepared, run: Tui, host: Host, metadata: Mapping[str, str]) -> dict:
    spec = prepared.spec
    return {
        "runtime": spec.runtime,
        "workdir": spec.workdir,
        **prepared.info.persisted(),
        "events": spec.events,
        "words": {label: WORDS[kind] for label, kind in run.hooks.items() if kind in WORDS},
        "host": host.name,
        "generation": metadata["generation"],
        **host.hook_state(spec.name or "", metadata),
        **run.hook_state(),
    }


def _bad(state: Mapping) -> str | None:
    """The first session.json field its readers refuse."""
    for field, check in tui_hook.SESSION_STATE.items():
        if field not in state or not check(state[field]):
            return field
    generation = state.get("generation")
    if not isinstance(generation, str) or not generation.isprintable() or not generation:
        return "generation"
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


def _read_state(name: str, path: str) -> dict:
    try:
        state = tui_hook.read_state(path, error=HookError)
    except HookError as e:
        raise DriverError(f"no tui session {name}: {e}") from None
    if field := _bad(state):
        raise DriverError(f"no tui session {name}: {path}: bad {field}")
    return state


class _Store:
    """Tui session `name`'s dir and lock under TUI_DIR; what changes them takes the lock."""

    def __init__(self, name: str, *, host: Host) -> None:
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
        return _read_state(self.name, self.state_file)

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

    def process(self) -> HostProcess | None:
        if self.name not in self.host.list():
            return None
        return self.host.attach(self.name)

    def holder(self) -> str | None:
        process = self.process()
        return None if process is None else process.identity

    def start(
        self,
        state: dict,
        commit: Callable[[], None],
        launch: Callable[[], Terminal],
        *,
        check_name: Callable[[str], None] | None = None,
    ) -> Terminal:
        if field := _bad(state):
            raise DriverError(f"tui session {self.name}: bad {field} {state.get(field)!r:.80}")
        with self._locked():
            if check_name is not None:
                check_name(self.name)
            made = self._make(state)
            try:
                commit()
                return launch()
            except (ConfigError, DriverError):
                self._unmake(made)
                raise

    def close(self) -> None:
        with self._locked():
            self._close()

    def stop(self, identity: str) -> None:
        with self._locked():
            holder = self.holder()
            if holder is None:
                self._drop()
            elif holder == identity:
                self._close()

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
        stale = _stale(self)
        if stale is not None:
            stale.close()
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

    def _close(self) -> None:
        try:
            process = self.process()
            if process is not None:
                process.close()
        except DriverError as error:
            raise DriverError(f"cannot close tui session {self.name}: {error}") from error
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
            # A hook may outlive its terminal; retire its public path before deleting files.
            with tempfile.TemporaryDirectory(dir=self.root, prefix=".closed-") as retired:
                os.rename(self.dir, retired)

    def _unmake(self, made: os.stat_result) -> None:
        with contextlib.suppress(OSError):
            self._remove(made)


def _stale(store: _Store) -> HostProcess | None:
    """Tui session `store.name`'s dead process, else None; DriverError while it runs."""
    process = store.process()
    if process is not None and process.alive():
        host = store.host
        hint = f"; {host.attach_command(store.name)}" if isinstance(host, Displayable) else ""
        raise DriverError(f"tui session {store.name} is running{hint}")
    return process


@dataclass(frozen=True)
class _Start:
    prepared: Prepared
    run: Tui
    store: _Store
    state: dict
    metadata: Mapping[str, str]

    def validate(self, display: Display) -> None:
        self.store.host.validate(self.store.name, self.launch, display=display)

    @property
    def launch(self) -> Launch:
        return Launch(self.prepared.argv, env=self.prepared.env, cwd=self.prepared.info.cwd)


class TuiMode(RunningMode):
    """One tui session holding its terminal generation."""

    kind = Mode.TUI

    @staticmethod
    def resolve_name(spec: Spec) -> str:
        if spec.sid is None:
            raise ConfigError("sid: missing")
        return checked_name(f"{spec.runtime}-{spec.sid[:8]}" if spec.name is None else spec.name)

    def __init__(
        self,
        prepared: Prepared,
        run: Tui,
        terminal: Terminal,
        store: _Store,
        generation: str,
        *,
        offset: int = 0,
        replayed: tuple[SessionEvent | Stderr | ClientBound, ...] = (),
    ) -> None:
        """offset: its hook log's next record."""
        super().__init__(prepared)
        self.name, self.run, self.host, self._terminal = store.name, run, store.host, terminal
        self._store, self._replayed, self._generation = store, replayed, generation
        self._offset, self._dialogs, self._limit, self._code = offset, frozenset(), None, None
        self._live, self._gone = True, False

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        host: Host,
        display: Display = None,
    ) -> Prepared:
        pending = cls._prepare(spec, adapter, registry=registry, host=host, display=display)
        try:
            pending.validate(display)
            _stale(pending.store)
        except DriverError as error:
            raise ConfigError(str(error)) from error
        return pending.prepared

    @classmethod
    def _prepare(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        host: Host,
        display: Display,
    ) -> _Start:
        run = adapter.tui()
        if run is None:
            raise cls._unsupported(spec)
        name = cls.resolve_name(spec)
        events = host.events_path(spec.events)
        cls._selected(spec, adapter)
        drop = identity_drop(registry)
        store = _Store(name, host=host)
        hooks = (*host.protected_paths(), *([events] if events else []), store.root)
        spec = replace(spec, name=name, events=events, protect=(*spec.protect, *hooks))
        cls.check_protection(spec, adapter)
        spec = cls._noted(spec, adapter)
        roots = module_roots(agent_pm.session, agent_pm.utils)
        hook = store.hook_argv(roots)
        prepared = cls._launched(spec, lambda s: run.launch(s, hook, source_roots=roots), drop)
        metadata = {
            "sid": prepared.info.sid,
            "runtime": spec.runtime or "session",
            "generation": uuid.uuid4().hex,
        }
        state = _state(prepared, run, host, metadata)
        if field := _bad(state):
            raise DriverError(f"tui session {name}: bad {field} {state.get(field)!r:.80}")
        return _Start(prepared, run, store, state, metadata)

    @classmethod
    def start(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        record: Callable[[SessionInfo], None],
        host: Host,
        display: Display = None,
        check_name: Callable[[str], None] | None = None,
    ) -> Self:
        """Start the Adapter's terminal with its hook journal committed before execution."""
        pending = cls._prepare(spec, adapter, registry=registry, host=host, display=display)
        prepared, run, store = pending.prepared, pending.run, pending.store
        try:
            pending.validate(display)
            host.prepare_events(prepared.spec.events)
        except DriverError as error:
            raise DriverError(f"cannot start tui session {store.name}: {error}") from error

        def launch() -> Terminal:
            try:
                return host.terminal(
                    store.name,
                    pending.launch,
                    display=display,
                    events=prepared.spec.events,
                    metadata=pending.metadata,
                )
            except DriverError as error:
                raise DriverError(f"cannot start tui session {store.name}: {error}") from error

        def commit() -> None:
            run.commit()
            record(prepared.info)

        terminal = store.start(pending.state, commit, launch, check_name=check_name)
        return cls(prepared, run, terminal, store, pending.state["generation"])

    @classmethod
    def snapshot(cls, name: str) -> dict:
        """Tui session `name`'s session.json as start wrote it; else DriverError."""
        return _read_state(
            checked_name(name), os.path.join(os.path.expanduser(TUI_DIR), name, tui_hook.STATE)
        )

    @classmethod
    def attach(cls, name: str, state: Mapping, adapter: Adapter, *, host: Host) -> Self:
        """Tui session `name` from its snapshot() if its terminal is tagged with its sid; a fresh Tui
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
        terminal = store.process()
        if terminal is not None and not isinstance(terminal, Terminal):
            raise DriverError(f"session {name} has no terminal")
        if terminal is None or terminal.session_identity() != (info.sid, state["generation"]):
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
            terminal,
            store,
            state["generation"],
            offset=offset,
            replayed=(*replayed,),
        )

    @classmethod
    def close(cls, name: str, *, host: Host) -> None:
        """Kills tui session `name` (absent is fine), removes its dir; under its lock."""
        _Store(name, host=host).close()

    @property
    def replayed(self) -> tuple[SessionEvent | Stderr | ClientBound, ...]:
        """Attached: its hook log's events so far."""
        return self._replayed

    def marked_blocked(self) -> bool:
        return self._terminal.activity() == Word.BLOCKED

    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        found = self._read()
        if not found and not self._gone:
            state = self._terminal.state()
            if state != Liveness.UNKNOWN:
                code = self._terminal.returncode()
                if state == Liveness.ABSENT:
                    code = -signal.SIGHUP
                if code is not None:
                    self._code = code
                    final = self._read()
                    self._end()
                    return final, True
                if self._terminal.alive() and isinstance(self.host, Displayable):
                    with contextlib.suppress(DriverError):
                        screen = self.host.screen(self._terminal)
                        found = self._read()
                        if not found and not self._gone:
                            found = self._scan(screen)
        deadline = time.monotonic() + timeout
        while not found and not self._gone and (left := deadline - time.monotonic()) > 0:
            time.sleep(min(SLICE, left))
            found = self._read()
        return found, self._gone

    def _end(self) -> None:
        self._gone = True
        self._code = -signal.SIGHUP if self._code is None else self._code

    def _read(self) -> list[SessionEvent | Stderr | ClientBound]:
        if self._gone:
            return []
        if self._terminal.state() == Liveness.ABSENT:
            replacement = self._store.process()
            if replacement is not None and replacement.identity != self._terminal.identity:
                self._end()
                return []
        try:
            current = self._store.read()
        except DriverError:
            self._end()
            return []
        if current["generation"] != self._generation:
            self._end()
            return []
        records, end = self._store.records(self._offset)
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
        with contextlib.suppress(DriverError):
            if self._terminal.activity() != Word.BLOCKED:
                self._terminal.mark(Word.BLOCKED, self.prepared.spec.events)

    def returncode(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while self._code is None:
            self._code = self._terminal.returncode()
            if self._terminal.state() == Liveness.ABSENT:
                self._code = -signal.SIGHUP
            if self._code is None:
                if (left := deadline - time.monotonic()) <= 0:
                    return None
                time.sleep(min(SLICE, left))
        return self._code

    def state(self) -> Liveness:
        return self._terminal.state()

    def send(self, text: str) -> None:
        self._running()
        for action in self.run.submit(text):
            self._terminal.send(action)

    def screen(self, lines: int | None = None) -> str:
        self._running()
        if not isinstance(self.host, Displayable):
            raise DriverError(f"host {self.host.name} cannot display")
        return self.host.screen(self._terminal, lines)

    def _running(self) -> None:
        if not self._terminal.alive():
            raise DriverError(f"tui session {self.name} is not running")

    def stop(self) -> None:
        """Once; then ends (returncode: the code seen, else -SIGHUP)."""
        if self._live:
            self._store.stop(self._terminal.identity)
        self._live = False
        self._end()
