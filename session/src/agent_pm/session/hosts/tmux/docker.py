"""DockerHost runs each tui session in its own broker-made container (docs/docker-host.md): the
container's tmux through `docker exec` (a Tmux over daemon.exec_runner), a host run supervisor for
its hooks, and a host view session, tagged VIEW, for show. What the host knows of a session is its
Host record (RECORD in its state dir), never the container's tmux options.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import replace

import agent_pm.utils
from agent_pm.session.engine.types import (
    COMMAND,
    SESSION_NAME,
    ClientHome,
    ConfigError,
    HookCallback,
    Launch,
    SessionError,
    SessionRow,
    StatusState,
)
from agent_pm.session.hosts.base import TUI_DIR, Host, Process, Showable
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
    docker_call,
    endpoint,
    exec_runner,
)
from agent_pm.session.hosts.docker.mounts import check_cwd, state_dir
from agent_pm.session.hosts.docker.relay import SOCKET
from agent_pm.session.hosts.tmux import docker_hook
from agent_pm.session.hosts.tmux.host import TmuxHost, TmuxTerminal
from agent_pm.session.hosts.tmux.transport import DIED, VIEW, Tmux, absent
from agent_pm.session.hosts.tmux.types import PaneStatus, TuiError
from agent_pm.utils import (
    Display,
    Record,
    Status,
    command_path,
    docker_home,
    module_roots,
    read_beneath,
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
PS_FORMAT = '{{.Names}}\t{{.Label "agent-pm.session"}}\t{{.Label "agent-pm.generation"}}'


def container_name(name: str, generation: str) -> str:
    return f"agent-pm-{name}-{generation}"


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


def _listed(containers: Mapping[str, tuple[str, str]], display: Display) -> list[str]:
    if display == "":
        return []
    return [
        name
        for name in sorted(containers)
        if display is None or TmuxHost.listed(display, _record(name).get("display"))
    ]


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
    ) -> None:
        self.name, self.tmux, self._process, self._control = name, tmux, process, None
        _, _, self._pane, self._pane_pid = process.split(" ")
        self.host, self.container, self.generation = host, container, generation

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
        """Kills the client (pane-died disarmed), the supervisor, the container, then the view."""
        failures = []
        res = self.tmux.run(
            [
                *("set-hook", "-p", "-u", "-t", self._pane, "pane-died", ";"),
                *("kill-session", "-t", f"={self.name}"),
            ]
        )
        if res.returncode and not absent(res):
            failures.append(f"kill: {res.stderr.strip()}")
        if why := _stop_supervisor(self.name):
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
    state = state_dir(name)
    lock = os.path.join(state, "supervisor.lock")
    if _lock_free(lock):
        return None
    try:
        with open(os.path.join(state, "supervisor.pid"), encoding="utf-8") as f:
            pid = int(f.read())
    except (OSError, ValueError):
        pid = None
    for sig, wait in ((signal.SIGTERM, LOCK_WAIT), (signal.SIGKILL, KILL_WAIT)):
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

    def _containers(self) -> dict[str, tuple[str, str]]:
        return {
            row[1]: (row[0], row[2])
            for row in self._ps()
            if len(row) == 3
            and SESSION_NAME.fullmatch(row[1])
            and row[0] == container_name(row[1], row[2])
        }

    def _client_env(
        self, name: str, client: str, home: ClientHome | None, *, resume: bool
    ) -> Mapping[str, str]:
        if home is None:
            raise ConfigError(
                f"client {client or '(unnamed)'}: not supported on the Docker Host yet"
            )
        return {home.env: docker_home(name, home.dir)}

    def _resume_argv(self, name: str) -> tuple[str, ...]:
        return (COMMAND, "resume", name)

    def validate(self, name: str, launch: Launch, *, display: Display = None) -> None:
        super().validate(name, launch, display=display)
        roots = [os.path.realpath(os.path.expanduser(root)) for root in self.roots]
        check_cwd(os.path.realpath(launch.cwd), roots, os.path.realpath(account_home()))

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
        self.validate(name, launch, display=display)
        events = self.prepare_events(self.events_path(events))
        metadata = metadata or {}
        generation, field, home_dir = (
            metadata.get(key) for key in ("generation", "home_field", "home_dir")
        )
        if not (generation and field and home_dir):
            raise SessionError(f"docker session {name}: no client home to mount")
        resume = metadata.get("resume")
        if resume not in ("0", "1"):
            raise SessionError(f"docker session {name}: bad resume flag {resume!r:.20}")
        if any(row[1:2] == [name] for row in self._ps()):
            raise SessionError(
                f"docker session {name}: a container for it exists; close {name} first"
            )
        home = docker_home(name, home_dir)
        if resume == "0":
            shutil.rmtree(home, ignore_errors=True)
            os.makedirs(home, mode=0o700)
        elif read_beneath(home, launch.transcript, limit=0) is None:
            raise ConfigError(f"docker session {name}: no native history to resume in {home}")
        hook = command_path(COMMAND)
        if hook is None:
            raise ConfigError(f"docker session {name}: {COMMAND} is not on PATH (its hooks run it)")
        self.broker.version()
        root = plugin_root(hook)
        clients = client_keys(root)
        image, _ = self.broker.build(root)
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
        container = container_name(name, generation)
        began = timestamp()
        try:
            started = self.broker.start(manifest, clients=clients)
        except BrokerError as error:
            if error.cleanup == "failed" and error.container:
                with contextlib.suppress(SessionError):
                    self.broker.close(error.container)
            raise
        ended = timestamp()
        supervisor = None
        try:
            if (started.endpoint, started.container) != (endpoint(), container):
                raise SessionError(
                    f"broker: started {started.container} at {started.endpoint},"
                    f" want {container} at {endpoint()}"
                )
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
                },
            )
            supervisor = self._supervise(name, container, events, hook)
            tmux = self.container_tmux(container)
            if not _wait(lambda: tmux.runner(["test", "-S", SOCKET]).returncode == 0, READY_WAIT):
                raise SessionError(f"no hook relay in {container} after {READY_WAIT} s")
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
        except (SessionError, TuiError, OSError) as error:
            cleanup = self._roll_back(supervisor, container)
            raise SessionError(f"docker session {name}: {error}{cleanup}") from error
        return DockerTerminal(
            name, process, tmux=tmux, host=self, container=container, generation=generation
        )

    def _supervise(self, name: str, container: str, events: str | None, hook: str):
        def execs(*args: str) -> str:
            return json.dumps(docker_call(self.docker, "exec", *args)[0])

        dead = ("display-message", "-p", "-t", f"={name}:", "#{pane_dead}")
        roots = module_roots(sys.modules[__name__], agent_pm.utils)
        proc = subprocess.Popen(
            [
                os.path.realpath(sys.executable),
                *("-P", "-s", "-m", run_supervisor.__name__),
                *(name, state_dir(name), os.path.join(os.path.expanduser(TUI_DIR), name)),
                *(events or "", hook),
                execs("-i", container, *RELAY),
                execs(container, "tmux", "wait-for", DEAD_SIGNAL),
                execs(container, "tmux", *dead),
            ],
            env={**docker_call(self.docker)[1], **source_path(roots)},
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        threading.Thread(target=proc.wait, daemon=True).start()
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
        container, generation = found
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
            name, process, tmux=tmux, host=self, container=container, generation=generation
        )

    def list(self, display: Display = None) -> list[str]:
        return _listed(self._containers(), display)

    def rows(self, display: Display = None) -> list[SessionRow]:
        containers = self._containers()
        return [self._row(name, *containers[name]) for name in _listed(containers, display)]

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
            last_lines=row["last_lines"],
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
        return shlex.join(_attach_argv(self.docker, found[0], name))

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

    def screen(self, name: str | Process, lines: int | None = None) -> str:
        process = self.attach(name) if isinstance(name, str) else name
        if (
            not isinstance(process, DockerTerminal)
            or process.host is not self
            or not process.alive()
        ):
            raise SessionError(f"session {process.name} is not running on host {self.name}")
        try:
            return process.tmux.read(process._pane, lines)
        except TuiError as error:
            raise SessionError(f"docker session {process.name}: {error}") from None
