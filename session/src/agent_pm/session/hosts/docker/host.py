"""DockerHost runs each tui session in its own broker-made container (docs/docker-host.md): the
container's tmux through `docker exec` (a TmuxCli over daemon.exec_runner), a host run supervisor for
its hooks; the Viewer shows a session through `docker exec` (a CommandView). What the host knows of
a session is its Host record (RECORD in its state dir), never the container's tmux options.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from typing import NamedTuple

import agent_pm.utils
from agent_pm.session.engine.tmux_cli import (
    DIED,
    TmuxCli,
    TmuxPane,
    TuiError,
    absent,
    display_error,
)
from agent_pm.session.engine.tmux_cli import POLL as TMUX_POLL
from agent_pm.session.engine.types import (
    COMMAND,
    MARKS,
    RESUME,
    SESSION_NAME,
    ClientHome,
    ConfigError,
    HookCallback,
    Key,
    Launch,
    Liveness,
    PaneStatus,
    Paste,
    SessionError,
    SessionRow,
    StatusState,
)
from agent_pm.session.hosts.base import (
    TUI_DIR,
    TUI_STATE,
    Host,
    InteractiveProcess,
    Process,
    tui_lock,
)
from agent_pm.session.hosts.docker import hook as docker_hook
from agent_pm.session.hosts.docker import supervisor as run_supervisor
from agent_pm.session.hosts.docker.broker import (
    REFRESH,
    RELAY,
    Broker,
    BrokerError,
    client_keys,
    plugin_root,
    write_manifest,
)
from agent_pm.session.hosts.docker.daemon import (
    CLEARED,
    RUN_TIMEOUT,
    account_home,
    capped,
    container_name,
    docker_call,
    endpoint,
    exec_runner,
)
from agent_pm.session.hosts.docker.mounts import check_cwd, state_dir
from agent_pm.session.hosts.docker.relay import GEN, SOCKET
from agent_pm.session.hosts.types import CommandView
from agent_pm.utils import (
    Display,
    Record,
    command_path,
    docker_home,
    inode_lock,
    module_roots,
    read_beneath,
    read_regular,
    save,
    source_path,
    timestamp,
)

KIND = "docker"
DEAD_SIGNAL = "agent-pm-dead"
RECORD = "host.json"
READY_WAIT = 10
LOCK_WAIT = 30
KILL_WAIT = 5
POLL = 0.2
LABEL = "agent-pm.session"
PS_FORMAT = "\t".join(
    (
        "{{.Names}}",
        '{{.Label "agent-pm.session"}}',
        '{{.Label "agent-pm.generation"}}',
        "{{.State}}",
    )
)
LOG = logging.getLogger("session")
STATE_MAX = 1 << 20
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
NO_SESSION = re.compile(r"can't find session.*|no server running on /.*", re.S)
STOPS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)


class Row(NamedTuple):
    """A `docker ps` row whose labels name a session and generation and match its name."""

    name: str
    container: str
    generation: str
    state: str


def _attach_argv(docker: str, container: str, name: str) -> list[str]:
    return docker_call(docker, "exec", "-it", container, "tmux", "attach", "-t", f"={name}")[0]


def _record(name: str) -> dict:
    try:
        with open(os.path.join(state_dir(name), RECORD), encoding="utf-8") as f:
            found = json.load(f)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _save_record(name: str, data: Mapping) -> None:
    save(os.path.join(state_dir(name), RECORD), json.dumps(dict(data)), durable=True)


def _take_lock(name: str) -> int | None:
    """supervisor.lock's fd, flocked and its stale pid file gone; None while another holds it."""
    state = state_dir(name)
    fd = os.open(os.path.join(state, "supervisor.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    with contextlib.suppress(FileNotFoundError):
        os.unlink(os.path.join(state, "supervisor.pid"))
    return fd


def _pid(name: str) -> int | None:
    try:
        with open(os.path.join(state_dir(name), "supervisor.pid"), encoding="utf-8") as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def _lock_free(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def _state_file(name: str) -> str:
    return os.path.join(os.path.expanduser(TUI_DIR), name, TUI_STATE)


def _image_digest(row: Row) -> str | None:
    """The image digest row's generation recorded at start."""
    try:
        with open(
            os.path.join(state_dir(row.name), f"{row.generation}.result.json"), encoding="utf-8"
        ) as f:
            found = json.load(f)
    except (OSError, ValueError):
        return None
    return found.get("image_digest") if isinstance(found, dict) else None


def _report_plan(name: str, plan: Mapping) -> None:
    """Freeze a generation's plan beside its manifest, then print it to stderr, a line a part."""
    try:
        save(
            os.path.join(state_dir(name), f"{plan['generation']}.plan.json"),
            json.dumps(plan),
            durable=True,
        )
    except OSError as error:
        raise SessionError(f"docker session {name}: plan: {error}") from None
    mounts = ", ".join(f"{mount['path']} rw" for mount in plan["mounts"])
    for part in (
        f"image {plan['image']} {plan['image_digest']}",
        f"mounts {mounts}",
        f"credential {plan['credential']}",
    ):
        print(f"docker plan {name} {plan['generation']}: {part}", file=sys.stderr)


def _text(found: str) -> str:
    """Container text, which reaches the host terminal, without C0 or C1 controls but tab."""
    return CONTROL.sub("", found)


class _Stops:
    """Holds STOPS but ignored ones while a start owns its container, so its rollback still runs:
    check() raises for one received, which is raised again on exit. Catches, not blocks, so no child
    inherits them blocked; only the main thread can set handlers.
    """

    def __enter__(self) -> _Stops:
        self.received: list[int] = []
        main = threading.current_thread() is threading.main_thread()
        self.previous = {
            sig: signal.signal(sig, lambda signum, _: self.received.append(signum))
            for sig in STOPS
            if main and signal.getsignal(sig) is not signal.SIG_IGN
        }
        return self

    def __exit__(self, *_) -> None:
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)
        if self.received:
            signal.raise_signal(self.received[0])

    def check(self, prefix: str = "") -> None:
        if self.received:
            raise SessionError(f"{prefix}stopped by {signal.Signals(self.received[0]).name}")


class _Laps:
    """Seconds per phase, since the previous one ended (docs/docker-host.md › Start timing)."""

    def __init__(self) -> None:
        self.phases: dict[str, float] = {}
        self._last = time.monotonic()

    def __call__(self, phase: str) -> None:
        now = time.monotonic()
        self.phases[phase] = round(now - self._last, 2)
        self._last = now

    def report(self, head: str) -> None:
        for phase, seconds in self.phases.items():
            print(f"{head}: {phase} {seconds:.2f} s", file=sys.stderr)


def _wait(ready, timeout: float, poll: float = POLL) -> bool:
    deadline = time.monotonic() + timeout
    while not ready():
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


class DockerTerminal(InteractiveProcess):
    """The container's pane, a TmuxPane on the container's TmuxCli. Its Record, display and sid: the
    Host record's, of the container's generation.
    """

    def __init__(
        self,
        name: str,
        process: str,
        *,
        tmux: TmuxCli,
        host: DockerHost,
        container: str,
        generation: str,
        current: bool = True,
    ) -> None:
        self.name, self.tmux, self.pane = name, tmux, TmuxPane(name, process, tmux)
        self._pane_pid = process.split(" ")[3]
        self.host, self.container, self.generation = host, container, generation
        self.current = current

    @property
    def identity(self) -> str:
        return self.pane.process

    def state(self) -> Liveness:
        return self.pane.state()

    def returncode(self) -> int | None:
        return self.pane.returncode()

    def activity(self) -> StatusState | None:
        value = self.pane.option("@state")
        return StatusState(value) if value in MARKS else None

    def alive(self) -> bool:
        return self.pane.alive()

    def send(self, action: Paste | Key) -> None:
        self.pane.send(action, self.alive)

    def press(self, keys) -> None:
        self.pane.press(keys, self.alive)

    def wait_exit(self, timeout: float) -> bool:
        return _wait(lambda: not (self.alive() or self._group_live()), timeout, TMUX_POLL)

    def kill(self) -> None:
        self._kill_group()
        if not self.wait_exit(KILL_WAIT):
            raise SessionError(f"process group {self.name}: did not exit")

    def _own_record(self) -> dict:
        found = _record(self.name)
        return found if found.get("generation") == self.generation else {}

    def _update(self, **changes) -> None:
        found = self._own_record()
        if not found:
            raise SessionError(f"docker session {self.name} is gone")
        _save_record(self.name, {**found, **changes})

    def publish(self, record: Record) -> None:
        if not isinstance(record, Record):
            raise ConfigError(f"record: want a Record, got {type(record).__name__}")
        self._update(record=record.to_line())

    def record(self) -> Record | None:
        line = self._own_record().get("record")
        with contextlib.suppress(ValueError, TypeError):
            return None if line is None else Record.parse(line)
        return None

    def display(self) -> Display:
        value = self._own_record().get("display")
        return value if isinstance(value, str) else None

    def session_identity(self) -> tuple[str | None, str | None]:
        found = self._own_record()
        return found.get("sid"), found.get("generation")

    def screen(self, lines: int | None = None) -> str:
        if not self.alive():
            raise SessionError(f"session {self.name} is not running on host {self.host.name}")
        try:
            return _text(self.pane.read(lines))
        except TuiError as error:
            raise SessionError(f"docker session {self.name}: {error}") from None

    def mark(self, word: StatusState, events: str | None) -> None:
        self.pane.mark(word, events, session=self.name)

    def _signal(self, sig: str) -> bool:
        res = self.tmux.runner(["kill", f"-{sig}", "--", f"-{self._pane_pid}"])
        return res.returncode == 0

    def _group_live(self) -> bool:
        return self._signal("0")

    def _kill_group(self) -> None:
        self._signal("KILL")

    def close(self) -> None:
        """Kills the client (pane-died disarmed), the supervisor (unless the image is another
        version's), then the container.
        """
        self.host.sweep(self.name)
        failures = []
        try:
            res = self.tmux.run(
                [
                    *("set-hook", "-p", "-u", "-t", self.pane.id, "pane-died", ";"),
                    *("kill-session", "-t", f"={self.name}"),
                ]
            )
        except TuiError as error:
            failures.append(f"kill: {error}")
        else:
            if res.returncode and not absent(res):
                failures.append(f"kill: {res.stderr.strip()}")
        if self.current and (why := _stop_supervisor(self.name)):
            failures.append(why)
        try:
            self.host.broker.close(self.container)
        except SessionError as error:
            failures.append(str(error))
        if failures:
            raise SessionError(f"docker session {self.name}: {'; '.join(failures)}")


def _stop_supervisor(name: str) -> str | None:
    """Why supervisor.lock is still held after SIGTERM, then SIGKILL, to its holder; else None."""
    lock = os.path.join(state_dir(name), "supervisor.lock")
    if _lock_free(lock):
        return None
    pid = None
    for sig, wait in ((signal.SIGTERM, LOCK_WAIT), (signal.SIGKILL, KILL_WAIT)):
        pid = _pid(name)
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        if _wait(lambda: _lock_free(lock), wait):
            return None
    return f"supervisor {pid}: still holds {lock}"


class DockerHost(Host):
    """settings: [hosts.<name>]'s docker (the CLI's absolute path) and mount_roots (the dirs a
    session's cwd may lie under). Only terminal sessions run here.
    """

    _inherit_env = False  # the container gets only the launch's own env (D10)
    attachable = True

    def __init__(
        self,
        name: str,
        settings: Mapping | None = None,
        *,
        run=capped,
        broker: Broker | None = None,
    ) -> None:
        super().__init__(name, settings)
        if extra := sorted(set(self.settings) - {"docker", "mount_roots"}):
            raise ConfigError(f"unknown key {extra[0]!r} in [hosts.{name}]")
        self.docker = self.settings.get("docker")
        if not isinstance(self.docker, str) or not os.path.isabs(self.docker):
            raise ConfigError(f"[hosts.{name}] docker: want an absolute path")
        self.roots = self.settings.get("mount_roots", [])
        if not isinstance(self.roots, list | tuple) or not all(
            isinstance(root, str) for root in self.roots
        ):
            raise ConfigError(f"[hosts.{name}] mount_roots: want a list of paths")
        self.run = run
        self.broker = Broker() if broker is None else broker
        self._warned: set[str] = set()

    def container_tmux(self, container: str) -> TmuxCli:
        return TmuxCli(exec_runner(self.docker, container, run=self.run))

    def _ps(self) -> list[list[str]]:
        """[] with no daemon socket or docker CLI, as tmux with no server: every Host is listed."""
        if not os.path.exists(endpoint().removeprefix("unix://")):
            return []
        argv, env = docker_call(
            self.docker, "ps", "-a", "--filter", f"label={LABEL}", "--format", PS_FORMAT
        )
        try:
            res = self.run(argv, env, timeout=RUN_TIMEOUT)
        except FileNotFoundError:
            return []
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(f"docker ps: {error}") from None
        if res.returncode:
            raise SessionError(f"docker ps: {res.stderr.strip()}")
        return [line.split("\t") for line in res.stdout.splitlines()]

    def _rows(self) -> list[Row]:
        return [
            Row(name, container, generation, state)
            for container, name, generation, state in (
                row for row in self._ps() if len(row) == len(Row._fields)
            )
            if SESSION_NAME.fullmatch(name)
            and GEN.fullmatch(generation)
            and container == container_name(name, generation)
        ]

    def _containers(self) -> dict[str, Row]:
        """Name → its row; of several, the one of host.json's generation."""
        found: dict[str, Row] = {}
        for row in self._rows():
            if row.name not in found or row.generation == _record(row.name).get("generation"):
                found[row.name] = row
        return found

    def sweep(self, own: str) -> None:
        """Closes leftover containers: ones whose session has no session.json, another generation
        in it, or (running) a tmux that positively answers it has no such session. Other names are
        judged only under their tui lock taken without waiting; `own`'s caller holds its lock.
        """
        try:
            rows = self._rows()
        except SessionError as error:
            LOG.warning("docker sweep: %s", error)
            return
        for row in rows:
            try:
                if row.name == own:
                    self._sweep_row(row)
                    continue
                lock = tui_lock(row.name)
                with inode_lock(lock, wait=False) as held:
                    if held:
                        try:
                            self._sweep_row(row)
                        finally:
                            if not os.path.lexists(_state_file(row.name)):
                                os.unlink(lock)
            except (SessionError, OSError) as error:
                LOG.warning("docker sweep %s: %s", row.name, error)

    def _sweep_row(self, row: Row) -> None:
        path = _state_file(row.name)
        if os.path.lexists(path):
            try:
                found = json.loads(read_regular(path, limit=STATE_MAX) or b"")
            except (ValueError, RecursionError):
                return
            generation = found.get("generation") if isinstance(found, dict) else None
            if not isinstance(generation, str) or (
                generation == row.generation
                and not (row.state == "running" and self._no_session(row))
            ):
                return
        self.broker.close(row.container)

    def _no_session(self, row: Row) -> bool:
        """tmux in row's container answered that it has no session row.name; a timeout or any other
        answer is no proof.
        """
        argv, env = docker_call(
            self.docker, "exec", "-i", row.container, "tmux", "has-session", "-t", f"={row.name}"
        )
        try:
            res = self.run(argv, env, timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return res.returncode == 1 and NO_SESSION.fullmatch(res.stderr.strip()) is not None

    def _recover(self, row: Row) -> bool:
        """False when row's image is not the one the current plugin builds. With its live container
        host.json's generation and supervisor.lock free, it checks that, then restarts the relay
        daemon and the supervisor on that lock; a failure only logs.
        """
        record = _record(row.name)
        if row.state != "running" or record.get("generation") != row.generation:
            return True
        state = state_dir(row.name)
        stale = os.path.join(state, f"{row.generation}.stale")
        lock = None
        try:
            if not _lock_free(os.path.join(state, "supervisor.lock")):
                return True
            hook = None
            if not os.path.lexists(stale):
                hook, _, (_, digest) = self._image(row.name, max_age=None)
                if _image_digest(row) != digest:
                    save(stale, digest)
            if hook is None or os.path.lexists(stale):
                if row.name not in self._warned:
                    self._warned.add(row.name)
                    LOG.warning(
                        "docker session %s runs an older image; restart it to upgrade",
                        row.name,
                    )
                return False
            lock = _take_lock(row.name)
            if lock is None:
                return True
            os.ftruncate(lock, 0)
            os.write(lock, row.generation.encode())
            self._serve_relay(row.container, row.generation)
            events = record.get("events")
            self._spawn_supervisor(
                row.name,
                row.container,
                generation=row.generation,
                events=events if isinstance(events, str) else None,
                hook=hook,
                lock=lock,
            )
        except (ConfigError, SessionError, OSError) as error:
            why = str(error).removeprefix(f"docker session {row.name}: ")
            LOG.warning("docker session %s: no supervisor restart: %s", row.name, why)
        finally:
            if lock is not None:
                os.close(lock)
        return True

    def _client_env(
        self, name: str, client: str, home: ClientHome | None, *, resume: bool
    ) -> Mapping[str, str]:
        if home is None:
            raise ConfigError(
                f"client {client or '(unnamed)'}: not supported on the Docker Host yet"
            )
        return {home.env: docker_home(name, home.dir)}

    def _resume_argv(self, name: str) -> tuple[str, ...]:
        return (*RESUME, name)

    def validate(self, name: str, launch: Launch) -> None:
        """The preflight, before any container: the cwd, the broker handshake and the build."""
        laps = _Laps()
        self._check_cwd(name, launch)
        self._image(name)
        laps("preflight")
        laps.report(f"docker timing {name}")

    def _check_cwd(self, name: str, launch: Launch) -> None:
        super().validate(name, launch)
        cwd = os.path.realpath(launch.cwd)
        if not os.path.isdir(cwd):
            raise ConfigError(f"Docker cwd {cwd}: no such directory")
        roots = [os.path.realpath(os.path.expanduser(root)) for root in self.roots]
        check_cwd(cwd, roots, os.path.realpath(account_home()))

    def _image(
        self, name: str, *, max_age: int | None = REFRESH
    ) -> tuple[str, str, tuple[str, str]]:
        """(the hook path, the plugin root, the built image and digest); `max_age`: Broker.build's."""
        hook = command_path(COMMAND)
        if hook is None:
            raise ConfigError(f"docker session {name}: {COMMAND} is not on PATH (its hooks run it)")
        self.broker.version()
        root = plugin_root(hook)
        return hook, root, self.broker.build(root, max_age=max_age)

    def detach(self, name, launch, *, display=None, events=None, record=None) -> Process:
        raise SessionError(f"detach {name}: the Docker Host runs only terminal sessions")

    def terminal(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> DockerTerminal:
        with _Stops() as stops:
            return self._terminal(
                name,
                launch,
                stops,
                display=display,
                events=events,
                record=record,
                metadata=metadata,
            )

    def _terminal(
        self,
        name: str,
        launch: Launch,
        stops: _Stops,
        *,
        display: Display,
        events: str | None,
        record: Record | None,
        metadata: Mapping[str, str] | None,
    ) -> DockerTerminal:
        if why := display_error(display):
            raise ConfigError(why)
        laps = _Laps()
        self._check_cwd(name, launch)
        events = self.prepare_events(self.events_path(events))
        metadata = metadata or {}
        generation, field, home_dir, credential = (
            metadata.get(key) for key in ("generation", "home_field", "home_dir", "credential")
        )
        if not (generation and field and home_dir and credential):
            raise SessionError(f"docker session {name}: no client home to mount")
        resume = metadata.get("resume")
        if resume not in ("0", "1"):
            raise SessionError(f"docker session {name}: bad resume flag {resume!r:.20}")
        self.sweep(name)
        if any(row[1:2] == [name] for row in self._ps()):
            raise SessionError(
                f"docker session {name}: a container for it exists; close {name} first"
            )
        home = docker_home(name, home_dir)
        if resume == "0":
            shutil.rmtree(home, ignore_errors=True)
            os.makedirs(home, mode=0o700)
            for file, text in json.loads(metadata.get("home_files") or "{}").items():
                save(os.path.join(home, file), text)
        elif read_beneath(home, launch.transcript, limit=0) is None:
            raise ConfigError(f"docker session {name}: no native history to resume in {home}")
        laps("checks")
        hook, root, (image, digest) = self._image(name)
        clients = client_keys(root)
        cwd = os.path.realpath(launch.cwd)
        manifest = os.path.join(state_dir(name), f"{generation}.json")
        write_manifest(
            manifest,
            name=name,
            generation=generation,
            image=image,
            cwd=cwd,
            home=(field, home),
            hook_path=hook,
        )
        _report_plan(
            name,
            {
                "name": name,
                "generation": generation,
                "image": image,
                "image_digest": digest,
                "mounts": [{"path": cwd, "rw": True}, {"path": home, "rw": True}],
                "credential": credential,
            },
        )
        laps("image")
        container = container_name(name, generation)
        stops.check(f"docker session {name}: ")
        began = timestamp()
        try:
            started = self.broker.start(manifest, clients=clients)
        except BrokerError as error:
            if error.cleanup == "failed" and error.container:
                with contextlib.suppress(SessionError):
                    self.broker.close(error.container)
            raise
        except SessionError as error:  # a timeout or bad response: the container may exist
            cleanup = self._roll_back(None, container)
            raise SessionError(f"docker session {name}: {error}{cleanup}") from error
        ended = timestamp()
        laps("start")
        supervisor = lock = None
        try:
            stops.check()
            if (started.endpoint, started.container) != (endpoint(), container):
                raise SessionError(
                    f"broker: started {started.container} at {started.endpoint},"
                    f" want {container} at {endpoint()}"
                )
            if why := _stop_supervisor(name):
                raise SessionError(why)
            lock = _take_lock(name)
            if lock is None:
                raise SessionError(f"{state_dir(name)}/supervisor.lock: held")
            os.ftruncate(lock, 0)
            os.write(lock, generation.encode())
            sid = metadata.get("sid") or (record.sid if record is not None else None)
            _save_record(
                name,
                {
                    "generation": generation,
                    "sid": sid,
                    "client": metadata.get("client"),
                    "display": display,
                    "record": None if record is None else record.to_line(),
                    "events": events,
                },
            )
            self._serve_relay(container, generation)
            tmux = self.container_tmux(container)
            if not _wait(lambda: tmux.runner(["test", "-S", SOCKET]).returncode == 0, READY_WAIT):
                raise SessionError(f"no hook relay in {container} after {READY_WAIT} s")
            laps("relay")
            supervisor = self._spawn_supervisor(
                name, container, generation=generation, events=events, hook=hook, lock=lock
            )
            process = tmux.launch(
                name,
                ["with-secrets", *launch.argv],
                cwd=cwd,
                env={k: v for k, v in launch.env.items() if k != "PATH"},
                tags={"@kind": KIND, "@sid": sid or "", "@generation": generation},
                died=f"{DIED} ; wait-for -S {DEAD_SIGNAL}",
            )
            _save_record(name, {**_record(name), "process": process})
            laps("tmux")
            if not _wait(lambda: tmux.status(name) == PaneStatus.RUNNING, READY_WAIT):
                raise SessionError(f"{container}: the client did not start in {READY_WAIT} s")
            laps("client")
            launched = datetime.now().astimezone().isoformat(timespec="milliseconds")
            result = {
                "container": started.container,
                "image": image,
                "image_digest": started.image_digest,
                "client_versions": dict(started.clients),
                "started_at": began,
                "ended_at": ended,
                "phases": laps.phases,
                "launched_at": launched,
            }
            save(
                os.path.join(state_dir(name), f"{generation}.result.json"),
                json.dumps(result),
                durable=True,
            )
            stops.check()
        except (SessionError, TuiError, OSError) as error:
            cleanup = self._roll_back(supervisor, container)
            raise SessionError(f"docker session {name}: {error}{cleanup}") from error
        finally:
            if lock is not None:
                os.close(lock)
        laps.report(f"docker timing {name} {generation}")
        return DockerTerminal(
            name, process, tmux=tmux, host=self, container=container, generation=generation
        )

    def _serve_relay(self, container: str, generation: str) -> None:
        argv, env = docker_call(self.docker, "exec", "-d", container, RELAY, "serve", generation)
        try:
            res = self.run(argv, env, timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(f"relay serve in {container}: {error}") from None
        if res.returncode:
            raise SessionError(f"relay serve in {container}: {res.stderr.strip()}")

    def _spawn_supervisor(
        self,
        name: str,
        container: str,
        *,
        generation: str,
        events: str | None,
        hook: str,
        lock: int,
    ):
        def execs(*args: str) -> str:
            return json.dumps(docker_call(self.docker, "exec", *args)[0])

        dead = ("display-message", "-p", "-t", f"={name}:", "#{pane_dead}")
        roots = module_roots(sys.modules[__name__], agent_pm.utils)
        proc = subprocess.Popen(
            [
                os.path.realpath(sys.executable),
                *("-P", "-s", "-m", run_supervisor.__name__),
                *(name, state_dir(name), os.path.join(os.path.expanduser(TUI_DIR), name)),
                *(events or "", hook, generation, str(lock)),
                execs("-i", container, RELAY, "connect"),
                execs(container, "tmux", "wait-for", DEAD_SIGNAL),
                execs(container, "tmux", *dead),
            ],
            env={**docker_call(self.docker)[1], **source_path(roots)},
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(lock,),
        )
        threading.Thread(target=proc.wait, daemon=True).start()
        save(os.path.join(state_dir(name), "supervisor.pid"), str(proc.pid))
        return proc

    def _roll_back(self, supervisor, container: str) -> str:
        errors = []
        if supervisor is not None:
            supervisor.terminate()
            try:
                supervisor.wait(KILL_WAIT)
            except subprocess.TimeoutExpired:
                supervisor.kill()
        try:
            self.broker.close(container)
        except SessionError as error:
            errors.append(f"close {container}: {error}")
        return "".join(f"; {error}" for error in errors)

    def hook_callback(self, name: str, metadata: Mapping[str, str]) -> HookCallback:
        generation = metadata["generation"]
        return HookCallback(
            docker_hook.__name__,
            {
                "docker": self.docker,
                "endpoint": endpoint(),
                "container": container_name(name, generation),
                "target": name,
                "generation": generation,
            },
        )

    def attach(self, name: str) -> DockerTerminal:
        found = self._containers().get(name)
        if found is None:
            raise SessionError(f"no docker session {name}")
        current = self._recover(found)
        container, generation = found.container, found.generation
        tmux = self.container_tmux(container)
        record = _record(name)
        process = record.get("process") if record.get("generation") == generation else None
        if not isinstance(process, str):
            try:
                process = tmux.process(name)
            except TuiError as error:
                raise SessionError(f"docker session {name}: {error}") from None
        if process is None:
            raise SessionError(f"no docker session {name}: its container runs no client")
        return DockerTerminal(
            name,
            process,
            tmux=tmux,
            host=self,
            container=container,
            generation=generation,
            current=current,
        )

    def list(self) -> list[str]:
        return sorted(self._containers())

    def rows(self) -> list[SessionRow]:
        """[] with a warning when docker fails, so the other Hosts' rows still list; list() raises,
        as its callers act on absence.
        """
        try:
            containers = self._containers()
        except SessionError as error:
            LOG.warning("docker rows: %s", error)
            return []
        return [
            self._row(name, containers[name].container, containers[name].generation)
            for name in sorted(containers)
        ]

    def _row(self, name: str, container: str, generation: str) -> SessionRow:
        record = _record(name)
        record = record if record.get("generation") == generation else {}
        found = [
            row for row in self.container_tmux(container).listing() or [] if row["name"] == name
        ]
        row = found[0] if len(found) == 1 else {"dead": True, "state": None, "last_lines": None}
        state = row["state"] if row["state"] in (StatusState.DONE, StatusState.BLOCKED) else None
        return SessionRow(
            name=name,
            sid=record.get("sid"),
            client=record.get("client"),
            state=StatusState.DEAD
            if row["dead"] is not False
            else StatusState(state or StatusState.WORKING),
            display=record.get("display"),
            shown=None,
            last_lines=None if row["last_lines"] is None else _text(row["last_lines"]),
            own=False,
        )

    def attach_target(self, name: str) -> CommandView:
        found = self._containers().get(name)
        if found is None:
            raise SessionError(f"no docker session {name}")
        argv = _attach_argv(self.docker, found.container, name)
        return CommandView(
            printed=shlex.join(argv),
            argv=("/usr/bin/env", *(arg for key in CLEARED for arg in ("-u", key)), *argv),
        )
