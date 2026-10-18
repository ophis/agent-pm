"""DockerHost runs each tui or headless session in its own broker-made container
(docs/docker-host.md): a tui one's tmux through `docker exec` (a TmuxCli over daemon.exec_runner), a
host run supervisor for its hooks; a headless one's client through `docker exec -i` (pipe). The
Viewer shows a session through `docker exec` (a CommandView). What the host knows of a session is
its Host record (RECORD in its state dir), never the container's tmux options.
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
    ClientRuntime,
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
    capped,
    container_name,
    docker_call,
    endpoint,
    exec_runner,
)
from agent_pm.session.hosts.docker.mounts import (
    KIND_LABEL,
    WORKSPACE_LABEL,
    state_dir,
    workspace_volume,
)
from agent_pm.session.hosts.docker.pipe import DockerPiped
from agent_pm.session.hosts.docker.relay import GEN, SOCKET
from agent_pm.session.hosts.docker.types import Started
from agent_pm.session.hosts.types import CommandView
from agent_pm.utils import (
    DOCKER_STATE,
    Display,
    Record,
    command_path,
    docker_home,
    inode_lock,
    kill_group,
    module_roots,
    piped_popen,
    read_beneath,
    read_regular,
    save,
    source_path,
    timestamp,
)

KIND = "docker"
DEAD_SIGNAL = "agent-pm-dead"
RECORD = "host.json"
PIPE_LOCK = "pipe.lock"  # held by a headless run's harness while its client runs
PIPED = "pipe"
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
KEPT = f"{DOCKER_STATE}/.kept"  # a marker per run volume kept: unsaved work, or a failed check
KEPT_LOCK = ".lock"
KEPT_RETRY = 24 * 3600  # seconds before a volume whose check failed is checked again
VOLUME_FORMAT = '{{.Name}}\t{{.Label "' + WORKSPACE_LABEL + '"}}'
STATE_MAX = 1 << 20
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
NO_SESSION = re.compile(r"can't find session.*|no server running on /.*", re.S)
PLUGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?")
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


def _piped(name: str, generation: str) -> bool:
    found = _record(name)
    return found.get("generation") == generation and found.get("mode") == PIPED


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
    workspace = plan["workspace"]
    for part in (
        f"image {plan['image']} {plan['image_digest']}",
        f"workspace {workspace['volume']} at {workspace['path']} rw",
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


class _Container(NamedTuple):
    container: str
    generation: str
    hook: str
    image: str
    cwd: str
    started: Started
    began: str
    ended: str


def _save_result(
    name: str, created: _Container, phases: Mapping[str, float], launched: str | None = None
) -> None:
    result = {
        "container": created.started.container,
        "image": created.image,
        "image_digest": created.started.image_digest,
        "client_versions": dict(created.started.clients),
        "started_at": created.began,
        "ended_at": created.ended,
        "phases": dict(phases),
    }
    if created.started.client_update is not None:
        result["client_update"] = dict(created.started.client_update)
    if launched is not None:
        result["launched_at"] = launched
    save(
        os.path.join(state_dir(name), f"{created.generation}.result.json"),
        json.dumps(result),
        durable=True,
    )


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


def _accept_started(name: str, generation: str, started: Started, container: str) -> None:
    """The broker started `container` here; its GitHub token expiries saved for refresh."""
    if (started.endpoint, started.container) != (endpoint(), container):
        raise SessionError(
            f"broker: started {started.container} at {started.endpoint},"
            f" want {container} at {endpoint()}"
        )
    save(
        os.path.join(state_dir(name), f"{generation}.credential.json"),
        json.dumps(started.credential_expires_at),
        durable=True,
    )
    save(
        os.path.join(state_dir(name), f"{generation}.github.json"),
        json.dumps(list(started.github)),
        durable=True,
    )


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
        self.host.reclaim()
        if failures:
            raise SessionError(f"docker session {self.name}: {'; '.join(failures)}")


def _held(marker: str) -> bool:
    """Whether a kept marker still holds its volume back: a failed check for KEPT_RETRY, anything
    else for good.
    """
    try:
        with open(marker) as f:
            failed = f.read() == "failed"
        return not failed or time.time() - os.stat(marker).st_mtime < KEPT_RETRY
    except FileNotFoundError:
        return False


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
    """settings: [hosts.<name>]'s docker, the CLI's absolute path; plugins, the account's installed
    plugins its image holds (none when absent); and github.app, the GitHub App whose tokens every
    session gets ("" or no table: none). Terminal and piped sessions run here, each on its
    workspace volume.
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
        if extra := sorted(set(self.settings) - {"docker", "github", "plugins"}):
            removed = "; see docs/upgrading.md" if extra[0] == "mount_roots" else ""
            raise ConfigError(f"unknown key {extra[0]!r} in [hosts.{name}]{removed}")
        self.docker = self.settings.get("docker")
        if not isinstance(self.docker, str) or not os.path.isabs(self.docker):
            raise ConfigError(f"[hosts.{name}] docker: want an absolute path")
        plugins = self.settings.get("plugins", [])
        if (
            not isinstance(plugins, list)
            or not all(isinstance(p, str) and PLUGIN.fullmatch(p) for p in plugins)
            or len(set(plugins)) != len(plugins)
        ):
            raise ConfigError(f"[hosts.{name}] plugins: want a list of unique plugin names")
        self.plugins: tuple[str, ...] = tuple(plugins)
        github = self.settings.get("github", {"app": ""})
        if not isinstance(github, Mapping):
            raise ConfigError(f"[hosts.{name}] github: want a table")
        if extra := sorted(set(github) - {"app"}):
            raise ConfigError(f"unknown key {extra[0]!r} in [hosts.{name}.github]")
        app = github.get("app")
        if not isinstance(app, str):
            raise ConfigError(f"[hosts.{name}.github] app: want a string")
        self.github = app
        self.run = run
        self.broker = Broker() if broker is None else broker
        self._warned: set[str] = set()

    def container_tmux(self, container: str) -> TmuxCli:
        return TmuxCli(exec_runner(self.docker, container, run=self.run))

    def _ps(self) -> list[list[str]]:
        """[] with no daemon socket or docker CLI, as tmux with no server: every Host is listed."""
        return self._listed("ps", "-a", "--filter", f"label={LABEL}", "--format", PS_FORMAT) or []

    def _listed(self, *args: str) -> list[list[str]] | None:
        """docker `args`' tab-separated rows; None with no daemon socket or docker CLI."""
        if not os.path.exists(endpoint().removeprefix("unix://")):
            return None
        argv, env = docker_call(self.docker, *args)
        try:
            res = self.run(argv, env, timeout=RUN_TIMEOUT)
        except FileNotFoundError:
            return None
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(f"docker {args[0]}: {error}") from None
        if res.returncode:
            raise SessionError(f"docker {args[0]}: {res.stderr.strip()}")
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
            if _piped(row.name, row.generation):
                continue
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

    def reclaim(self) -> None:
        """At a close, never a start: removes one run workspace volume whose workdir is gone, after
        the broker's check (up to its 120 s). One with unsaved work is kept for good, one whose check
        failed for KEPT_RETRY; each is warned about once and marked. Listed before the lock, so no
        orphan writes no file.
        """
        try:
            self._reclaim()
        except (SessionError, OSError) as error:
            LOG.warning("docker reclaim: %s", error)

    def _reclaim(self) -> None:
        rows = self._listed(
            "volume", "ls", "--filter", f"label={KIND_LABEL}", "--format", VOLUME_FORMAT
        )
        if rows is None:
            return
        gone = [
            volume
            for volume, anchor in (row for row in rows if len(row) == 2)
            if os.path.isabs(anchor) and not os.path.lexists(anchor)
        ]
        kept = os.path.expanduser(KEPT)
        if not gone and not os.path.isdir(kept):
            return
        os.makedirs(kept, mode=0o700, exist_ok=True)
        with inode_lock(os.path.join(kept, KEPT_LOCK), wait=False) as held:
            if not held:
                return
            for marker in set(os.listdir(kept)) - {row[0] for row in rows} - {KEPT_LOCK}:
                os.unlink(os.path.join(kept, marker))
            volume = next((v for v in gone if not _held(os.path.join(kept, v))), None)
            if volume is None:
                return
            marker = os.path.join(kept, volume)
            try:
                self.broker.workspace_rm(volume)
            except BrokerError as error:
                save(marker, "dirty" if error.code == "workspace_dirty" else "failed")
                LOG.warning("docker workspace %s kept: %s", volume, error.message)
            except SessionError as error:
                save(marker, "failed")
                LOG.warning("docker workspace %s kept: %s", volume, error)
            else:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(marker)

    def _sweep_row(self, row: Row) -> None:
        if _piped(row.name, row.generation):
            if _lock_free(os.path.join(state_dir(row.name), PIPE_LOCK)):
                self.broker.close(row.container)
            return
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

    def _client_runtime(self) -> ClientRuntime:
        return ClientRuntime(
            "/opt/agent-pm/bin",
            "/opt/agent-pm/venv/bin/python",
            "/opt/agent-pm/session/src:/opt/agent-pm/utils/src",
            "/run/agent-pm",
        )

    def _client_env(
        self, name: str, client: str, home: ClientHome | None, *, resume: bool
    ) -> Mapping[str, str]:
        if home is None:
            raise ConfigError(
                f"client {client or '(unnamed)'}: not supported on the Docker Host yet"
            )
        path = docker_home(name, home.dir)
        if os.path.realpath(path) != path:
            raise ConfigError(f"client home {path}: symlink component")
        return {home.env: path}

    def _resume_argv(self, name: str) -> tuple[str, ...]:
        return (*RESUME, name)

    def _preflight(self, name: str, launch: Launch) -> None:
        self.validate(name, launch, missing_ok=True)

    def validate(self, name: str, launch: Launch, *, missing_ok: bool = False) -> None:
        """The preflight, before any container: the cwd (`missing_ok`: or none yet), the broker
        handshake and the build.
        """
        laps = _Laps()
        self._check_cwd(name, launch, missing_ok=missing_ok)
        self._image(name)
        laps("preflight")
        laps.report(f"docker timing {name}")

    def _check_cwd(self, name: str, launch: Launch, *, missing_ok: bool = False) -> None:
        super().validate(name, launch)
        cwd = os.path.realpath(launch.cwd)
        if not os.path.isdir(cwd) and not (missing_ok and not os.path.lexists(cwd)):
            raise ConfigError(f"Docker cwd {cwd}: no such directory")

    def _image(
        self, name: str, *, max_age: int | None = REFRESH
    ) -> tuple[str, str, tuple[str, str]]:
        """(the hook path, the plugin root, the built image and digest); `max_age`: Broker.build's."""
        hook = command_path(COMMAND)
        if hook is None:
            raise ConfigError(f"docker session {name}: {COMMAND} is not on PATH (its hooks run it)")
        self.broker.version()
        root = plugin_root(hook)
        return hook, root, self.broker.build(root, plugins=self.plugins, max_age=max_age)

    def _move_workspace(self, name: str, volume: str, cwd: str) -> None:
        """Deletes a new start's workspace volume mounted at another cwd, unless the broker's check
        finds unsaved work there; one at `cwd`, or none, is kept for this start.
        """
        rows = self._listed(
            "volume", "ls", "--filter", f"label={KIND_LABEL}", "--format", VOLUME_FORMAT
        )
        anchor = next((row[1] for row in rows or () if len(row) == 2 and row[0] == volume), cwd)
        if anchor == cwd:
            return
        try:
            self.broker.workspace_rm(volume)
        except BrokerError as error:
            if error.code == "workspace_dirty":
                raise ConfigError(
                    f"docker session {name}: {error.message}; or start it in {anchor} again"
                ) from error
            raise

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
        created = self._container(name, launch, stops, laps, metadata)
        container, generation, hook, _, cwd, _, _, _ = created
        supervisor = lock = None
        try:
            stops.check()
            self._stage_files(container, metadata)
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
                    "credential_refresh": created.started.credential_expires_at is not None,
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
                [
                    "with-secrets",
                    "--client",
                    metadata["home_field"].removesuffix("_dir"),
                    *launch.argv,
                ],
                cwd=cwd,
                env=self._exec_env(launch),
                tags={"@kind": KIND, "@sid": sid or "", "@generation": generation},
                died=f"{DIED} ; wait-for -S {DEAD_SIGNAL}",
            )
            _save_record(name, {**_record(name), "process": process})
            laps("tmux")
            if not _wait(lambda: tmux.status(name) == PaneStatus.RUNNING, READY_WAIT):
                raise SessionError(f"{container}: the client did not start in {READY_WAIT} s")
            laps("client")
            launched = datetime.now().astimezone().isoformat(timespec="milliseconds")
            _save_result(name, created, laps.phases, launched)
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

    def _container(
        self, name: str, launch: Launch, stops: _Stops, laps: _Laps, metadata: Mapping[str, str]
    ) -> _Container:
        """The checks, image, workspace, home and plan, then the broker's start, rolled back on a
        transport failure: what terminal and pipe share.
        """
        generation, field, home_dir, credential = (
            metadata.get(key) for key in ("generation", "home_field", "home_dir", "credential")
        )
        if not (generation and field and home_dir and credential):
            raise SessionError(f"docker session {name}: no client home to mount")
        resume = metadata.get("resume")
        if resume not in ("0", "1"):
            raise SessionError(f"docker session {name}: bad resume flag {resume!r:.20}")
        self.sweep(name)
        for row in self._ps():
            if row[1:2] == [name]:
                if len(row) == len(Row._fields) and _piped(name, row[2]):
                    raise SessionError(
                        f"docker session {name}: a headless run holds it;"
                        " wait for it to end or stop its run"
                    )
                raise SessionError(
                    f"docker session {name}: a container for it exists; close {name} first"
                )
        home = docker_home(name, home_dir)
        if resume == "1" and read_beneath(home, launch.transcript, limit=0) is None:
            raise ConfigError(f"docker session {name}: no native history to resume in {home}")
        laps("checks")
        hook, root, (image, digest) = self._image(name)
        volume = workspace_volume(name)
        cwd = os.path.realpath(launch.cwd)
        persistent = json.loads(metadata.get("home_persistent") or "null")
        if resume == "0":
            self._move_workspace(name, volume, cwd)
            shutil.rmtree(home, ignore_errors=True)
            os.makedirs(home, mode=0o700)
            for file, text in json.loads(metadata.get("home_files") or "{}").items():
                save(os.path.join(home, file), text)
            for directory in persistent or ():
                os.makedirs(os.path.join(home, directory), mode=0o700, exist_ok=True)
        clients = client_keys(root)
        manifest = os.path.join(state_dir(name), f"{generation}.json")
        write_manifest(
            manifest,
            name=name,
            generation=generation,
            image=image,
            cwd=cwd,
            workspace=volume,
            home=(field, home),
            hook_path=hook,
            github=self.github,
        )
        _report_plan(
            name,
            {
                "name": name,
                "generation": generation,
                "image": image,
                "image_digest": digest,
                "workspace": {"volume": volume, "path": cwd},
                "mounts": [
                    {"path": os.path.join(home, directory), "rw": True} for directory in persistent
                ]
                if persistent is not None
                else [{"path": home, "rw": True}],
                "credential": credential,
            },
        )
        laps("image")
        container = container_name(name, generation)
        stops.check(f"docker session {name}: ")
        began = timestamp()
        try:
            started = self.broker.start(manifest, clients=clients)
            _accept_started(name, generation, started, container)
            ended = timestamp()
            laps("start")
            created = _Container(container, generation, hook, image, cwd, started, began, ended)
            _save_result(name, created, laps.phases)
        except BrokerError as error:
            if error.cleanup == "failed" and error.container:
                with contextlib.suppress(SessionError):
                    self.broker.close(error.container)
            raise
        except (SessionError, OSError) as error:
            cleanup = self._roll_back(None, container)
            raise SessionError(f"docker session {name}: {error}{cleanup}") from error
        return created

    def pipe(
        self, name: str | None, launch: Launch, *, metadata: Mapping[str, str] | None = None
    ) -> DockerPiped:
        """`docker exec -i` of the client in its own container, no tmux, relay or supervisor; it
        holds the tui lock while it starts, then its pipe lock while the client runs.
        """
        if name is None:
            raise ConfigError("a headless session on the Docker Host needs a name")
        for key in launch.env:
            if key in CLEARED or key.startswith("DOCKER_"):
                raise ConfigError(
                    f"docker session {name}: env {key} would reconfigure the docker CLI"
                )
        lock = tui_lock(name)
        os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
        with _Stops() as stops, inode_lock(lock, wait=False) as held:
            if not held:
                raise SessionError(f"docker session {name}: busy")
            try:
                return self._pipe(name, launch, stops, metadata or {})
            finally:
                if not os.path.lexists(_state_file(name)):
                    os.unlink(lock)

    def _pipe(
        self, name: str, launch: Launch, stops: _Stops, metadata: Mapping[str, str]
    ) -> DockerPiped:
        laps = _Laps()
        self._check_cwd(name, launch)
        created = self._container(name, launch, stops, laps, metadata)
        container, generation, _, _, cwd, _, _, _ = created
        lock = proc = None
        try:
            stops.check()
            lock = os.open(os.path.join(state_dir(name), PIPE_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _save_record(
                name,
                {
                    "generation": generation,
                    "sid": metadata.get("sid"),
                    "client": metadata.get("client"),
                    "credential_refresh": created.started.credential_expires_at is not None,
                    "mode": PIPED,
                },
            )
            env = self._exec_env(launch)
            argv, docker_env = docker_call(
                self.docker,
                *("exec", "-i", "--workdir", cwd),
                *(arg for key in env for arg in ("--env", key)),
                *(
                    container,
                    "with-secrets",
                    "--client",
                    metadata["home_field"].removesuffix("_dir"),
                    *launch.argv,
                ),
            )
            proc = piped_popen(argv, env={**docker_env, **env})
            laps("client")
            _save_result(name, created, laps.phases)
            stops.check()
        except (SessionError, OSError, ValueError) as error:
            if proc is not None:
                kill_group(proc)
            if lock is not None:
                os.close(lock)
            cleanup = self._roll_back(None, container)
            raise SessionError(f"docker session {name}: {error}{cleanup}") from error
        laps.report(f"docker timing {name} {generation}")
        return DockerPiped(
            name,
            proc,
            close=lambda: self._close_piped(name, container),
            lock=lock,
            refresh=lambda stopped: run_supervisor.refresh_tokens(
                name,
                state_dir(name),
                generation,
                self.broker,
                stopped,
                failed=lambda reason: LOG.warning(
                    "docker session %s: %s %s",
                    name,
                    "client_refresh_failed"
                    if created.started.credential_expires_at is not None
                    else "github_refresh_failed",
                    reason,
                ),
                expired=lambda reason: LOG.warning(
                    "docker session %s: client_auth_expired %s", name, reason
                ),
            ),
        )

    def _stage_files(self, container: str, metadata: Mapping[str, str]) -> None:
        files = metadata.get("runtime_files")
        if not files or files == "{}":
            return
        argv, env = docker_call(
            self.docker,
            "exec",
            "-i",
            "--user",
            "agent",
            container,
            "/opt/agent-pm/venv/bin/python",
            "-I",
            "/opt/agent-pm/lib/bootstrap.py",
            "runtime-files",
        )
        try:
            result = self.run(argv, env, input=files, timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(f"staging client runtime: {error}") from None
        if result.returncode:
            raise SessionError("staging client runtime failed")

    def _exec_env(self, launch: Launch) -> dict[str, str]:
        return {
            **{k: v for k, v in launch.env.items() if k != "PATH"},
            **({"AGENT_PM_PR_AUTHOR": f"{self.github}[bot]"} if self.github else {}),
        }

    def _close_piped(self, name: str, container: str) -> None:
        try:
            self.broker.close(container)
        except SessionError as error:
            LOG.warning("docker session %s: %s", name, error)
        self.reclaim()

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
