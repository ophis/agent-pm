"""Deprecated wrappers, deleted by 356-14 (FR-27): Driver forwards to its own configured
HostedSession class, session_cls.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from agent_pm.session.adapters import registry
from agent_pm.session.engine.base import Session
from agent_pm.session.engine.config import read_config
from agent_pm.session.engine.hosted import HostedSession
from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.types import (
    ConfigError,
    DriverError,
    Hosts,
    Launch,
    Mode,
    NoRecordedSession,
    NotDisplayable,
    Resumable,
    SessionRow,
    SessionStatus,
    Spec,
    Usage,
)
from agent_pm.session.hosts.base import Displayable, Host
from agent_pm.session.hosts.tmux import TmuxHost, tmux_host
from agent_pm.session.modes import tui
from agent_pm.utils import load_class, printable_abs
from agent_pm.utils import probe as run_probe

Handle = Session


def note(workdir: str, kind: str, **fields) -> None:
    """Appends a `kind` record to <workdir>/run.jsonl."""
    RunLog(workdir).note(kind, **fields)


def session_record(workdir: str, sid: str) -> dict | None:
    """RunLog(workdir).session(sid)'s stored fields; None: no valid `session` line."""
    info = RunLog(workdir).session(sid)
    return None if info is None else info.persisted()


def sessions(match: str | None = None) -> list[SessionRow]:
    return HostedSession.list(match)


def close(name: str) -> None:
    """Kills tui session `name` (absent is fine), then removes its session dir."""
    Driver.from_config(read_config()).close(name)


def _on(tmux) -> Callable[[type[Host], str, Mapping], Host]:
    def build(cls: type[Host], name: str, settings: Mapping) -> Host:
        return cls(name, settings, tmux=tmux) if issubclass(cls, TmuxHost) else cls(name, settings)

    return build


def _opener(display) -> str | None:
    parts = TmuxHost.unfold(display)
    return None if parts is None else parts[0]


class Driver:
    """hosts: parsed configuration or tables with default_host; host: the execution transport;
    harness_host selects where detached runs live.
    """

    def __init__(
        self,
        *,
        adapters: Mapping,
        hosts: Hosts | Mapping,
        default_host: str | None = None,
        host=tmux_host,
        probe=run_probe,
        clock: Callable[[], float] | None = None,
        class_loader=load_class,
        harness_host: str | None = None,
    ):
        if isinstance(hosts, Hosts):
            if default_host is not None:
                raise ConfigError(
                    "default_host: give it with [hosts.<name>] tables, not parsed Hosts"
                )
        elif not isinstance(hosts, Mapping) or "default" in hosts:
            raise ConfigError("hosts: want a mapping of [hosts.<name>] tables")
        else:
            hosts = Host.parse_hosts({**hosts, "default": default_host})
        self.host, self.probe, self.clock = host, probe, clock
        self.harness_host = harness_host
        self._built = (host, probe, clock)
        self._session_cls = HostedSession.configured(
            adapters=adapters, hosts=hosts, class_loader=class_loader, **self._collaborators()
        )
        self.registry = self._session_cls._config().registry

    @property
    def session_cls(self) -> type[HostedSession]:
        now = (self.host, self.probe, self.clock)
        if any(given is not built for given, built in zip(now, self._built, strict=True)):
            self._session_cls = self._session_cls._rebound(**self._collaborators())
            self._built = now
        return self._session_cls

    def _collaborators(self) -> dict:
        return dict(
            probe=self.probe,
            clock=time.monotonic if self.clock is None else self.clock,
            host_factory=_on(self.host),
        )

    @classmethod
    def from_config(
        cls,
        config: Mapping,
        *,
        hosts: Hosts | None = None,
        host=tmux_host,
        probe=run_probe,
        clock: Callable[[], float] | None = None,
        class_loader=load_class,
        harness_host: str | None = None,
    ):
        """config: a read session config; hosts: its [hosts] if the caller parsed it already."""
        return cls(
            **registry.settings(config, hosts=hosts),
            host=host,
            probe=probe,
            clock=clock,
            class_loader=class_loader,
            harness_host=harness_host,
        )

    def start(
        self,
        runtime: str,
        mode: str,
        prompt: str,
        spec: Spec,
        *,
        resume: str | None = None,
        events: str | None = None,
        idle_timeout: float | None = None,
    ) -> Handle:
        """spec.sid: its id (None: resume, else a new one); tui: as TuiMode.start says."""
        if mode not in tuple(Mode):
            raise ConfigError(f"mode must be one of {', '.join(Mode)}, got {mode!r}")
        if resume is not None and not (isinstance(resume, str) and resume):
            raise ConfigError(f"resume must be a session id, got {resume!r:.80}")
        if spec.sid is not None and resume is not None and spec.sid != resume:
            raise ConfigError(f"sid {spec.sid!r:.80} differs from resume {resume!r:.80}")
        return self.session_cls.start(
            replace(
                spec,
                runtime=runtime,
                mode=mode,
                prompt=prompt,
                sid=resume or spec.sid,
                resume=resume is not None,
                events=events,
            ),
            idle_timeout=idle_timeout,
        )

    def attach(self, name: str, idle_timeout: float | None = None) -> Handle:
        return self.session_cls.attach(name, idle_timeout=idle_timeout)

    def show(
        self,
        name: str,
        split: str | None = None,
        split_from: str | None = None,
        opener: str | None = None,
    ) -> str | None:
        tui.checked_name(name)
        display = TmuxHost.fold(opener, split, split_from)
        self.registry.tui()
        return self._shown().show(name, display=display)

    def usage(self, runtime: str) -> Usage | None:
        return self.session_cls.usage(runtime)

    @staticmethod
    def session_record(workdir: str, sid: str) -> dict | None:
        return session_record(workdir, sid)

    def session_status(self, runtime: str, sid: str, cwd: str, *, workdir: str) -> SessionStatus:
        """cwd retains legacy validation; a missing historical record is not resumable."""
        if not printable_abs(cwd):
            raise ConfigError(f"cwd must be a printable absolute path, got {cwd!r:.80}")
        try:
            return self.session_cls.inspect(Spec(workdir=workdir, runtime=runtime, sid=sid))
        except NoRecordedSession:
            return SessionStatus(Resumable.NO, None, "")

    def writable_dirs(self, runtime: str, spec: Spec, resume: str | None = None) -> tuple[str, ...]:
        """Resume: session `resume`'s. An OSError reading them is a ConfigError, so a caller fails
        closed.
        """
        if spec.sid is not None:
            raise ConfigError("spec.sid is set by writable_dirs(): pass resume=")
        return self.session_cls.writable_dirs(
            replace(spec, runtime=runtime, sid=resume, resume=resume is not None)
        )

    def sessions(self, match: str | None = None) -> list[SessionRow]:
        return self.session_cls.list(match)

    def close(self, name: str) -> None:
        tui.TuiMode.close(name, host=self.session_cls.host())

    def layout(self, split: str | None = None, split_from: str | None = None) -> str | None:
        """Legacy layout completion, retained until TASK-375."""
        return _opener(self.session_cls.layout(display=TmuxHost.fold(None, split, split_from)))

    def attach_command(self, name: str) -> str:
        name = tui.checked_name(name)
        return self._shown().attach_command(name)

    def detach(
        self,
        name: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        *,
        opener: str | None = None,
    ) -> None:
        """Runs argv on the selected harness Host and returns once argv has executed."""
        tui.checked_name(name)
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(isinstance(a, str) for a in argv)
        ):
            raise ConfigError(f"argv must be a non-empty list of str, got {argv!r:.80}")
        if not printable_abs(cwd):
            raise ConfigError(f"cwd must be a printable absolute path, got {cwd!r:.80}")
        if not os.path.isdir(cwd):
            raise ConfigError(f"cwd must be an existing directory, got {cwd!r:.80}")
        display = TmuxHost.fold(opener, None, None)
        try:
            launch = Launch(tuple(argv), cwd=cwd, env=env)
        except ConfigError as error:
            raise DriverError(
                f"detach {name}: cannot run {argv[0]}: {os.strerror(errno.EINVAL)}"
            ) from error
        self.session_cls.host(self.harness_host).detach(name, launch, display=display)

    def _shown(self) -> Displayable:
        host = self.session_cls.host()
        if not isinstance(host, Displayable):
            raise NotDisplayable(f"host {host.name} cannot display")
        return host
