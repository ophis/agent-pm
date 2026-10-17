"""DockerHost runs each tui session in its own broker-made container (docs/docker-host.md): the
container's tmux through `docker exec` (a Tmux over daemon.exec_runner), a host run supervisor for
its hooks, and a host view session, tagged VIEW, for show. What the host knows of a session is its
Host record (RECORD in its state dir), never the container's tmux options.
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
from dataclasses import replace
from typing import NamedTuple

import agent_pm.utils
from agent_pm.session.engine.types import (
    COMMAND,
    RESUME,
    SESSION_NAME,
    ClientHome,
    ConfigError,
    HookCallback,
    Launch,
    SessionError,
    SessionRow,
    StatusState,
)
from agent_pm.session.hosts.base import TUI_DIR, TUI_STATE, Host, Process, Showable, tui_lock
from agent_pm.session.hosts.docker import hook as docker_hook
from agent_pm.session.hosts.docker import supervisor as run_supervisor
from agent_pm.session.hosts.docker.broker import (
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
from agent_pm.session.hosts.tmux.host import TmuxHost, TmuxTerminal
from agent_pm.session.hosts.tmux.transport import DIED, VIEW, Tmux, absent
from agent_pm.session.hosts.tmux.types import PaneStatus, TuiError
from agent_pm.utils import (
    Display,
    Record,
    Status,
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


def _listed(containers: Mapping[str, Row], display: Display) -> list[str]:
    if display == "":
        return []
    return [
        name
        for name in sorted(containers)
        if display is None or TmuxHost.listed(display, _record(name).get("display"))
    ]


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


def _wait(ready, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not ready():
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL)
    return True


class DockerTerminal(TmuxTerminal):
    """Its Record, display, sid and hidden flag: the Host record's, of the container's generation."""

    def __init__(
        self,
        name: str,
        process: str,
        *,
        tmux: Tmux,
        host: DockerHost,
        container: str,
        generation: str,
        current: bool = True,
    ) -> None:
        self.name, self.tmux, self._process, self._control = name, tmux, process, None
        _, _, self._pane, self._pane_pid = process.split(" ")
        self.host, self.container, self.generation = host, container, generation
        self.current = current

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

    def hidden(self) -> bool:
        return self._own_record().get("hidden") is True

    def screen(self, lines: int | None = None) -> str:
        if not self.alive():
            raise SessionError(f"session {self.name} is not running on host {self.host.name}")
        try:
            return _text(self.tmux.read(self._pane, lines))
        except TuiError as error:
            raise SessionError(f"docker session {self.name}: {error}") from None

    def mark_hidden(self, *, hidden: bool) -> None:
        self._update(hidden=hidden)

    def mark(self, word: StatusState, events: str | None) -> None:
        if self._own():
            try:
                self.tmux.mark(word, events, pane=self._pane, session=self.name)
            except TuiError as error:
                raise SessionError(str(error)) from error

    def status(self) -> Status:
        found = Process.status(self)
        attach = shlex.join(_attach_argv(self.host.docker, self.container, self.name))
        return replace(found, attach=attach) if found.alive else found

    def _signal(self, sig: str) -> bool:
        res = self.tmux.runner(["kill", f"-{sig}", "--", f"-{self._pane_pid}"])
        return res.returncode == 0

    def _group_live(self) -> bool:
        return self._signal("0")

    def _kill_group(self) -> None:
        self._signal("KILL")

    def close(self) -> None:
        """Kills the client (pane-died disarmed), the supervisor (unless the image is another
        version's), the container, then the view.
        """
        self.host.sweep(self.name)
        failures = []
        try:
            res = self.tmux.run(
                [
                    *("set-hook", "-p", "-u", "-t", self._pane, "pane-died", ";"),
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
        try:
            if self.host.tmux.option(self.name, VIEW) == KIND:
                self.host.tmux.kill(self.name)
        except TuiError as error:
            failures.append(f"view: {error}")
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


class DockerHost(Host, Showable):
    """settings: [hosts.<name>]'s docker (the CLI's absolute path) and mount_roots (the dirs a
    session's cwd may lie under). Only terminal sessions run here.
    """

    _inherit_env = False  # the container gets only the launch's own env (D10)

    def __init__(
        self,
        name: str,
        settings: Mapping | None = None,
        *,
        tmux: Tmux | None = None,
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
        self.tmux = Tmux() if tmux is None else tmux
        self.view = TmuxHost(name, tmux=self.tmux)
        self.run = run
        self.broker = Broker() if broker is None else broker
        self._warned: set[str] = set()

    def container_tmux(self, container: str) -> Tmux:
        return Tmux(exec_runner(self.docker, container, run=self.run))

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
                hook, _, (_, digest) = self._image(row.name)
                if _image_digest(row) != digest:
                    save(stale, digest)
            if hook is None or os.path.lexists(stale):
                if row.name not in self._warned:
                    self._warned.add(row.name)
                    LOG.warning(
                        "docker session %s runs an image from an older agent-pm;"
                        " restart it to upgrade",
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

    def validate(self, name: str, launch: Launch, *, display: Display = None) -> None:
        """The preflight, before any container: the cwd, the broker handshake and the build."""
        self._check_cwd(name, launch, display=display)
        self._image(name)

    def _check_cwd(self, name: str, launch: Launch, *, display: Display) -> None:
        super().validate(name, launch, display=display)
        cwd = os.path.realpath(launch.cwd)
        if not os.path.isdir(cwd):
            raise ConfigError(f"Docker cwd {cwd}: no such directory")
        roots = [os.path.realpath(os.path.expanduser(root)) for root in self.roots]
        check_cwd(cwd, roots, os.path.realpath(account_home()))

    def _image(self, name: str) -> tuple[str, str, tuple[str, str]]:
        """(the hook path, the plugin root, the built image and digest)."""
        hook = command_path(COMMAND)
        if hook is None:
            raise ConfigError(f"docker session {name}: {COMMAND} is not on PATH (its hooks run it)")
        self.broker.version()
        root = plugin_root(hook)
        return hook, root, self.broker.build(root)

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
        self._check_cwd(name, launch, display=display)
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
                    "hidden": False,
                    "events": events,
                },
            )
            self._serve_relay(container, generation)
            tmux = self.container_tmux(container)
            if not _wait(lambda: tmux.runner(["test", "-S", SOCKET]).returncode == 0, READY_WAIT):
                raise SessionError(f"no hook relay in {container} after {READY_WAIT} s")
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
            if not _wait(lambda: tmux.status(name) == PaneStatus.RUNNING, READY_WAIT):
                raise SessionError(f"{container}: the client did not start in {READY_WAIT} s")
            result = {
                "container": started.container,
                "image": image,
                "image_digest": started.image_digest,
                "client_versions": dict(started.clients),
                "update": dict(started.update),
                "started_at": began,
                "ended_at": ended,
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

    def list(self, display: Display = None) -> list[str]:
        return _listed(self._containers(), display)

    def rows(self, display: Display = None) -> list[SessionRow]:
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
            for name in _listed(containers, display)
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

    def validate_display(self, value: Display) -> None:
        self.view.validate_display(value)

    def display(self, value: Display) -> Display:
        return self.view.display(value)

    def display_help(self) -> str:
        return self.view.display_help()

    def layout(self, *, display: Display = None) -> str | None:
        return self.view.layout(display=display)

    def tile(self, window: str, *, wait: bool = True, if_panes_changed: bool = False) -> None:
        self.view.tile(window, wait=wait, if_panes_changed=if_panes_changed)

    def attach_command(self, name: str) -> str:
        found = self._containers().get(name)
        if found is None:
            raise SessionError(f"no docker session {name}")
        return shlex.join(_attach_argv(self.docker, found.container, name))

    def show(self, name: str, *, display: Display = None, command: str | None = None) -> str | None:
        process = self.attach(name)
        try:
            if self.tmux.status(name) is None:
                self.tmux.checked(
                    [
                        *("new-session", "-d", "-s", name, "--", "/usr/bin/env"),
                        *(arg for key in CLEARED for arg in ("-u", key)),
                        *_attach_argv(self.docker, process.container, name),
                        *(";", "set-option", "-t", f"={name}:", VIEW, KIND),
                    ]
                )
            elif self.tmux.option(name, VIEW) != KIND:
                raise SessionError(f"show {name}: a host tmux session {name} exists")
        except TuiError as error:
            raise SessionError(f"show {name}: {error}") from None
        why = self.view.show(name, display=display, command=command)
        self._shown(process)
        return None if why is None else self.attach_command(name)

    def hide(self, name: str, *, if_closed: bool = False) -> None:
        process = self.attach(name)
        if self.tmux.status(name) is not None:
            self.view.hide(name, if_closed=if_closed)
        self._shown(process)

    def _shown(self, process: DockerTerminal) -> None:
        try:
            shown = self.view.placement.shown(process.name) is not None
        except TuiError as error:
            raise SessionError(f"show {process.name}: {error}") from None
        process.mark_hidden(hidden=not shown)
