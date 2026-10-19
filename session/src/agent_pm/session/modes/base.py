"""BaseMode, a Mode's running session, and what the modes share to start one. The class's
start() fixes a Prepared before anything starts, has `record` write the session line, then starts
and returns the session, so none exists unstarted. stop() is idempotent.
"""

import json
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import replace
from typing import ClassVar, Self

from agent_pm.session.clients.base import Client
from agent_pm.session.clients.common import check_selection
from agent_pm.session.clients.registry import Registry
from agent_pm.session.engine.types import (
    COMMAND,
    ClientBound,
    ClientHome,
    ConfigError,
    KeyPress,
    Launch,
    Liveness,
    Mode,
    Prepared,
    SessionError,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
)
from agent_pm.session.hosts.base import Host, host_resume
from agent_pm.utils import (
    AGENT_SESSION_ENV,
    CLIENT_ENV,
    EVENTS_ENV,
    LEASE_ENV,
    SID_ENV,
    Display,
    command_path,
    nested_start,
)


def child_env(
    launch: Launch,
    cwd: str,
    drop: Collection[str] = (),
    client: str | None = None,
    *,
    inherit: bool = True,
) -> dict[str, str]:
    """The environment (inherit; else none) less drop and unset, then the launch env, PWD,
    AGENT_SESSION_ENV and CLIENT_ENV (client, when given); PATH led by the running plugin's bin/,
    where PATH finds COMMAND (FR-32), when it does.
    """
    source = os.environ if inherit else {}
    env = {k: v for k, v in source.items() if k not in drop and k not in launch.unset}
    env = {**env, **launch.env, "PWD": cwd, AGENT_SESSION_ENV: "1"}
    if client is not None:
        env[CLIENT_ENV] = client
    command = command_path(COMMAND)
    if command is not None:
        plugin = os.path.dirname(command)
        path = env.get("PATH", os.defpath)
        if path.split(os.pathsep)[0] != plugin:
            env["PATH"] = plugin + os.pathsep + path
    return env


def identity_drop(registry: Registry) -> frozenset[str]:
    """SID_ENV and EVENTS_ENV (a caller's start token and events file, never a nested start's),
    LEASE_ENV (a manager's roster lease), each configured Client class's identity_env, and its
    child_defaults at their value inside it; builds no Client.
    """
    kinds = [registry.client_class(name) for name in registry.configured()]
    drop = {SID_ENV, EVENTS_ENV, *LEASE_ENV, *(key for kind in kinds for key in kind.identity_env)}
    for kind in kinds:
        if kind.session_env and kind.session_env in os.environ:
            drop.update(k for k, v in kind.child_defaults.items() if os.environ.get(k) == v)
    return frozenset(drop)


class BaseMode(ABC):
    kind: ClassVar[Mode]

    def __init__(self, prepared: Prepared) -> None:
        """Built by start (TuiMode.attach too) from what it started."""
        self._prepared = prepared

    @property
    def prepared(self) -> Prepared:
        return self._prepared

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        client: Client,
        *,
        registry: Registry,
        host: Host,
        display: Display = None,
    ) -> Prepared:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def start(
        cls,
        spec: Spec,
        client: Client,
        *,
        registry: Registry,
        record: Callable[[SessionInfo], None],
        host: Host,
        display: Display = None,
        check_name: Callable[[str], None] | None = None,
    ) -> Self:
        """The session started; spec.sid set; registry: tui settings and identity_drop; host: the
        selected execution Host; display: a tui session's, kept as given (other modes refuse one).
        """

    @property
    def opening_pending(self) -> str | None:
        """The launch input not yet sent (tui only), else None."""
        return None

    @abstractmethod
    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        """Events within `timeout` s and whether they ended; raises the mode's own failures."""

    @abstractmethod
    def returncode(self, timeout: float) -> int | None:
        """None while running; -N: killed by signal N."""

    @abstractmethod
    def state(self) -> Liveness: ...

    @abstractmethod
    def send(self, text: str) -> None: ...

    @abstractmethod
    def screen(self, lines: int | None = None) -> str: ...

    def press(self, keys: Sequence[KeyPress]) -> None:
        raise SessionError(f"press: a {self.kind.value} session has no terminal")

    @abstractmethod
    def stop(self) -> None: ...

    def close(self) -> None:
        """Ends the run for good; a tui one also drops its state."""
        self.stop()

    @classmethod
    def _unsupported(cls, spec: Spec) -> ConfigError:
        return ConfigError(f"client {spec.client!r} does not support {cls.kind.value} mode")

    @staticmethod
    def _no_tui_keys(spec: Spec, display: Display = None, *, named: bool = False) -> None:
        name = None if named else spec.name
        for key, value in (("name", name), ("events", spec.events), ("display", display)):
            if value is not None:
                raise ConfigError(f"{key}: tui mode only")

    @staticmethod
    def _hosted(
        spec: Spec, client_home: ClientHome | None, host: Host, name: str
    ) -> tuple[Spec, Mapping[str, str]]:
        """spec with `host`'s client env for `name`, and the metadata its Host keeps."""
        home = host._client_env(name, spec.client or "", client_home, resume=spec.resume)
        metadata = {
            "sid": spec.sid or "",
            "client": spec.client or "session",
            "generation": uuid.uuid4().hex,
            **(
                {
                    "home_field": client_home.field,
                    "home_dir": client_home.dir,
                    "credential": client_home.credential,
                    "home_files": json.dumps(dict(client_home.files)),
                    **(
                        {"home_providers": json.dumps(client_home.providers)}
                        if client_home.providers
                        else {}
                    ),
                    **(
                        {"home_persistent": json.dumps(client_home.persistent)}
                        if client_home.persistent is not None
                        else {}
                    ),
                    "resume": "1" if spec.resume else "0",
                }
                if home and client_home
                else {}
            ),
        }
        return replace(spec, env={**spec.env, **home}), metadata

    @staticmethod
    def _host_resume(prepared: Prepared, host: Host, name: str) -> Prepared:
        if resume := host_resume(host, name, prepared.info.cwd):
            prepared = replace(prepared, info=replace(prepared.info, resume=resume))
        return prepared

    @staticmethod
    def _selected(spec: Spec, client: Client) -> None:
        check_selection(spec.flags, client.forbidden_selection, client.valued_flags)

    @staticmethod
    def _noted(spec: Spec, client: Client) -> Spec:
        if spec.access.commands and client.client_note:
            return replace(spec, prompt=spec.prompt + "\n\n" + client.client_note)
        return spec

    @staticmethod
    def _launched(
        spec: Spec, launch: Callable[[Spec], Launch], drop: Collection[str], *, inherit: bool = True
    ) -> Prepared:
        if refused := nested_start():
            raise ConfigError(refused)
        if spec.sid is None:
            raise ConfigError("sid: missing")
        command = launch(spec)
        if not command.argv:
            raise ConfigError(f"client {spec.client!r} gave no command")
        cwd = command.cwd or spec.access.cwd
        project = spec.access.project if command.project is None else command.project
        info = SessionInfo(spec.sid, cwd, project, command.transcript, command.resume)
        env = child_env(command, cwd, drop, spec.client, inherit=inherit)
        return Prepared(spec, info, command.argv, env)
