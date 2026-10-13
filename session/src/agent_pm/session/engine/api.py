"""The Driver's API. start() checks everything, writes a session's `session` record to
<workdir>/run.jsonl, then starts it in its Mode (headless, tui or in_process) and returns its
Handle. Handle.events() streams its events, Stderr and IdleTimeout (none while Blocked; it stops
nothing) up to the Driver's own Exited, recording `client_session` and `stderr`. usage(),
session_status() and session_record() give a runtime's Usage, a session's SessionStatus and its
record; note() appends a record, holds() checks a file's text; writable_dirs() the dirs a runtime
grants itself write to under a Spec. attach(), sessions(), show() and close() reach a tui session by
name; detach() runs a command in a tmux session of its own.
"""

import logging
import os
import re
import signal
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace

from agent_pm.session.adapters import types as registry
from agent_pm.session.adapters.selection import check as check_selection
from agent_pm.session.engine import records
from agent_pm.session.engine.launch.types import STEPS, LaunchContext, _child_env
from agent_pm.session.engine.lifecycle.types import (
    Handle,
    HandleServices,
    HandleState,
    _diagnostics,
)
from agent_pm.session.engine.records import holds, session_record
from agent_pm.session.engine.runtime.types import DriverServices, StartOptions
from agent_pm.session.engine.runtime.validation import _idle, _name, _placement
from agent_pm.session.engine.types import (
    ConfigError,
    DriverError,
    SessionInfo,
    SessionRequest,
    SessionRow,
    SessionStatus,
    Spec,
    Usage,
    sid_error,
)
from agent_pm.session.hosts.tables import NAME
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.errors.types import TuiError
from agent_pm.session.modes.tui import types as tui
from agent_pm.utils import STATE, printable_abs

__all__ = [
    "Driver",
    "Handle",
    "MODES",
    "SIGNALS",
    "holds",
    "note",
    "session_record",
]

MODES = ("headless", "tui", "in_process")
PROBE_DIR = f"{STATE}/probe"
PROBE_TIMEOUT = 120
SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
LOG = logging.getLogger("driver")


def _row(found: Mapping, own: str | None) -> SessionRow:
    sid, runtime, state = found["sid"], found["runtime"], found["state"]
    return SessionRow(
        name=found["name"],
        sid=None if sid_error(sid) else sid,
        runtime=runtime if isinstance(runtime, str) and NAME.match(runtime) else None,
        state="dead" if found["dead"] else state if state in ("done", "blocked") else "working",
        pane=found["pane"],
        opener=found["opener"],
        shown=found["shown"],
        last_lines=found["last_lines"],
        own=found["name"] == own,
    )


def _probe_dir() -> str | None:
    path = os.path.expanduser(PROBE_DIR)
    try:
        os.makedirs(path, 0o700, exist_ok=True)
        return path if stat.S_ISDIR(os.lstat(path).st_mode) else None
    except OSError:
        return None


def note(workdir: str, kind: str, **fields) -> None:
    """Appends a `kind` record to <workdir>/run.jsonl."""
    records.note(records.path(workdir), kind, **fields)


class Driver:
    """A caller owns the configuration for every session it starts or attaches."""

    def __init__(
        self,
        *,
        adapters: Mapping,
        hosts: Mapping,
        default_host: str,
        services: DriverServices | None = None,
    ):
        services = DriverServices() if services is None else services
        self._logger = _diagnostics()
        self.host, self.probe, self.clock = services.host, services.probe, services.clock
        self.registry = registry.Registry(adapters=adapters, hosts=hosts, default_host=default_host)

    @classmethod
    def from_config(cls, config: Mapping, *, services: DriverServices | None = None):
        try:
            return cls(**registry.settings(config), services=services)
        except ValueError as error:
            raise ConfigError(str(error)) from error

    def start(
        self,
        runtime: str,
        mode: str,
        prompt: str,
        spec: Spec,
        *,
        options: StartOptions | None = None,
    ) -> Handle:
        """spec.sid: its id (None: options.resume, else a new one); tui: tmux session spec.name,
        else <runtime>-<sid[:8]>, refused when running; options.events gets done, blocked and dead
        lines; spec.protect gains the hook paths.
        """
        options = StartOptions() if options is None else options
        resume, idle_timeout, events = options.resume, options.idle_timeout, options.events
        if mode not in MODES:
            raise ConfigError.mode(MODES, mode)
        _idle(idle_timeout)
        if resume is not None and not (isinstance(resume, str) and resume):
            raise ConfigError.resume(resume)
        if spec.sid is not None and resume is not None and spec.sid != resume:
            raise ConfigError.resume_mismatch(spec.sid, resume)
        adapter = self.registry.adapter(runtime)
        kind = STEPS[mode]
        run = kind.run_of(adapter)
        if run is None:
            raise ConfigError.runtime_mode(runtime, mode)
        spec = replace(spec, sid=resume or spec.sid or str(uuid.uuid4()))
        steps = kind(
            run,
            LaunchContext(
                runtime=runtime, spec=spec, events=events, registry=self.registry, host=self.host
            ),
        )
        check_selection(spec.flags, adapter.forbidden_selection, adapter.valued_flags)
        spec = steps.specced(spec)
        if spec.access.commands and adapter.runtime_note:
            prompt = prompt + "\n\n" + adapter.runtime_note
        info = steps.info(prompt, SessionRequest(spec, resumed=resume is not None))
        with steps.prepared(info, spec.workdir) as host:
            os.makedirs(spec.workdir, exist_ok=True)
            records.note(
                records.path(spec.workdir),
                "session",
                sid=spec.sid,
                cwd=info.cwd,
                project=info.project,
                transcript=info.transcript,
                resume=info.resume,
            )
            steps.begin(host)
        return Handle(
            runtime,
            mode,
            replace(spec, name=steps.name),
            state=HandleState(info=info, idle_timeout=idle_timeout),
            services=HandleServices(
                host=host,
                adapter=adapter,
                usage=lambda: self.usage(runtime),
                clock=self.clock,
                logger=self._logger,
            ),
        )

    def attach(self, name: str, idle_timeout: float | None = None) -> Handle:
        """Tui session `name`'s Handle from any process: its reply the last turn's, its events from
        the next hook record.
        """
        _name(name)
        _idle(idle_timeout)
        state = tui.read_state(name)
        try:
            adapter = self.registry.adapter(state["runtime"])
            run = adapter.tui()
        except ConfigError as e:
            raise DriverError.no_tui_session(name, e) from None
        if run is None:
            raise DriverError.no_tui_runtime(name, state["runtime"])
        host = tui.TuiMode.attach(name, state, run, host=self.host)
        info = SessionInfo(
            state["sid"],
            host.bound,
            state["cwd"],
            state["project"],
            state["transcript"],
            state["resume"],
        )
        handle = Handle(
            state["runtime"],
            "tui",
            Spec(workdir=state["workdir"], sid=state["sid"], name=name),
            state=HandleState(
                info=info, idle_timeout=idle_timeout, reply=host.reply, blocked=host.blocked
            ),
            services=HandleServices(
                host=host,
                adapter=adapter,
                usage=lambda: self.usage(state["runtime"]),
                clock=self.clock,
            ),
        )
        handle.info()
        return handle

    def show(
        self,
        name: str,
        split: str | None = None,
        split_from: str | None = None,
        opener: str | None = None,
    ) -> str | None:
        """Opens a pane on session `name` per the tui settings; None, else logs the reason and
        returns attach_command(name).
        """
        _name(name)
        _placement(split, split_from, opener)
        settings = self.registry.tui()
        if self.host.status(name) is None:
            raise DriverError.no_tmux_session(name)
        why = self.host.open_pane(
            name,
            options=tmux_host.PaneOptions(
                split=split,
                split_from=split_from,
                opener=opener,
                per_column=settings.per_column,
                retile=settings.retile,
            ),
        )
        if why is None:
            return None
        LOG.warning("show %s: %s", name, why)
        return self.host.attach_command(name)

    def usage(self, runtime: str) -> Usage | None:
        """None only when the probe fails: no probe, a failed start or timeout, or no reading in its
        output.
        """
        adapter = self.registry.adapter(runtime)
        launch = adapter.usage_probe()
        if launch is None or not launch.argv:
            return None
        cwd = launch.cwd or _probe_dir()
        if cwd is None:
            return None
        output = self.probe(
            launch.argv, cwd=cwd, env=_child_env(launch, cwd), timeout=PROBE_TIMEOUT
        )
        if output is None:
            return None
        try:
            return adapter.usage(output)
        except Exception:
            self._logger.debug("Usage parsing failed for %s", runtime, exc_info=True)
            return None

    @staticmethod
    def session_record(workdir: str, sid: str) -> dict | None:
        return records.session_record(workdir, sid)

    def session_status(self, runtime: str, sid: str, cwd: str) -> SessionStatus:
        if error := sid_error(sid):
            raise ConfigError(error)
        if not printable_abs(cwd):
            raise ConfigError.cwd(cwd)
        adapter = self.registry.adapter(runtime)
        try:
            return adapter.session_status(sid, cwd)
        except OSError:
            return SessionStatus("unknown", None, "")

    def writable_dirs(self, runtime: str, spec: Spec, resume: str | None = None) -> tuple[str, ...]:
        """Resume: session `resume`'s. An OSError reading them is a ConfigError, so a caller fails
        closed.
        """
        if spec.sid is not None:
            raise ConfigError.writable_sid()
        if resume is not None and (error := sid_error(resume)):
            raise ConfigError(error)
        adapter = self.registry.adapter(runtime)
        try:
            return tuple(
                adapter.writable_dirs(
                    SessionRequest(replace(spec, sid=resume), resumed=resume is not None)
                )
            )
        except OSError as e:
            raise ConfigError.writable_directories(runtime, e) from e

    def sessions(self, match: str | None = None) -> list[SessionRow]:
        """Names re.fullmatching `match`, by name; DriverError, never [], when tmux's state is
        unknown.
        """
        if match is not None and not isinstance(match, str):
            raise ConfigError.match_type(match)
        try:
            pattern = None if match is None else re.compile(match)
        except re.error as e:
            raise ConfigError.invalid_match(match, e) from None
        found = self.host.listing(pattern)
        if found is None:
            raise DriverError.sessions_unavailable()
        try:
            own = self.host.own_session() if found else None
        except TuiError as e:
            raise DriverError.sessions_unavailable(e) from None
        return [_row(f, own) for f in found]

    def close(self, name: str) -> None:
        """Kills tui session `name` (absent is fine), then removes its session dir."""
        tui.close(_name(name), host=self.host)

    def layout(self, split: str | None = None, split_from: str | None = None) -> str | None:
        """show()'s opener once the pane it goes beside (split_from's, else the caller's) is found;
        None only with split_from.
        """
        _placement(split, split_from)
        if split_from is not None and self.host.status(split_from) is None:
            raise ConfigError.missing_tmux_session(split_from)
        try:
            self.host.anchor(split_from)
            try:
                return self.host.opener()
            except TuiError:
                if split_from is None:
                    raise
                return None
        except TuiError as e:
            raise DriverError.no_anchor(e, split_from) from None

    def attach_command(self, name: str) -> str:
        return self.host.attach_command(_name(name))

    def detach(
        self,
        name: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        *,
        opener: str | None = None,
    ) -> None:
        """Runs argv in new tmux session `name`, SIGNALS blocked until argv unblocks them; returns
        once argv has exec'd.
        """
        _name(name)
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(isinstance(a, str) for a in argv)
        ):
            raise ConfigError.argv(argv)
        if not printable_abs(cwd):
            raise ConfigError.cwd(cwd)
        if not os.path.isdir(cwd):
            raise ConfigError.cwd_directory(cwd)
        _placement(None, None, opener)
        try:
            self.host.detach(
                name,
                tmux_host.Command(argv=list(argv), cwd=cwd, env=dict(env)),
                options=tmux_host.DetachOptions(block=SIGNALS, opener=opener),
            )
        except TuiError as e:
            raise DriverError.detach(name, e) from e
