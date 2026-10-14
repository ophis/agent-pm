"""HarnessDelegate owns an accepted plan, its Composer, sessions and durable recovery identity."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace

import agent_pm.session as session_package
import agent_pm.utils as utils_package
from agent_pm.delegate import config
from agent_pm.delegate.composer import Composer, DelegateConfig, RunConfig, Team, teams
from agent_pm.delegate.engine.base import Delegate
from agent_pm.delegate.engine.callback import EventCallback
from agent_pm.delegate.engine.config import package_dirs
from agent_pm.delegate.engine.drivers import AgentDriver, Driver, InstructionDriver
from agent_pm.delegate.engine.plan import plan
from agent_pm.delegate.engine.runner import execute
from agent_pm.delegate.engine.types import (
    ConfigError,
    Event,
    Finished,
    Limit,
    Outer,
    Plan,
    Preview,
    Result,
    Spec,
    Stopped,
)
from agent_pm.delegate.event.types import Outcome
from agent_pm.session import (
    SIGNALS,
    Access,
    Displayable,
    DriverError,
    Host,
    HostProcess,
    Launch,
    Liveness,
    NotDisplayable,
    Session,
    SessionStatus,
    Usage,
)
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import (
    Spec as SessionSpec,
)
from agent_pm.utils import (
    Display,
    Record,
    Status,
    file_lock,
    import_env,
    load_class,
    read_regular,
    save,
    thaw,
    under,
)


def _team(team: Team) -> Team:
    return teams.team(team.root, team.dirs)


def _encode(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


@dataclass(frozen=True)
class _State:
    role: str
    plan: Plan
    session_cls: str
    host: str
    harness_host: str
    display: Display
    callback: tuple[str, ...] | None
    agent_name: str
    harness_name: str
    attempt: int
    outer: Outer
    overlays: tuple[Mapping, ...]
    detached: bool = False
    completed: bool = False
    result: Result | None = None


class HarnessDelegate(Delegate):
    def __init__(self, role: str, spec: Spec, **placement) -> None:
        self._accept(role, spec, **placement)
        try:
            self.driver.preflight(self._state.plan.session)
        except (SessionConfigError, DriverError) as error:
            raise ConfigError(str(error)) from None
        self._persist_new()

    @classmethod
    def preview(cls, role: str, spec: Spec, **placement) -> Preview:
        """What the constructor would accept, without the session preflight; writes nothing."""
        view = cls.__new__(cls)
        view._accept(role, spec, **placement)
        state = view._state
        return Preview(
            state.plan,
            state.agent_name if state.plan.spec.mode == "tui" else None,
            state.harness_name,
            state.host,
            state.harness_host,
            state.callback,
        )

    @classmethod
    def instruct(cls, role: str, spec: Spec) -> Result:
        """Delivers the role's prompt to spec.out: no Session, no delegate.json."""
        try:
            composer = Composer(config=DelegateConfig(config.read_config()), team=spec.team)
        except (OSError, ValueError) as error:
            raise ConfigError(str(error)) from None
        driver = InstructionDriver()
        accepted = plan(
            composer.resolve(role, spec.task),
            replace(spec, team=composer.team),
            driver=driver,
            composition=composer,
        )
        return execute(accepted, driver=driver)

    def _accept(
        self,
        role: str,
        spec: Spec,
        *,
        host: str | None = None,
        display: Display = None,
        harness_host: str | Host | None = None,
        callback: Sequence[str] | None = None,
        overlays: Sequence[Mapping] = (),
        session_cls: type[Session] = Session,
        composer: Composer | None = None,
        driver: Driver | None = None,
    ):
        try:
            self.composer = composer or Composer(
                config=DelegateConfig(config.read_config()), team=spec.team
            )
            self._settings = thaw(self.composer.config.values)
            self.session_cls = session_cls
            self._configure()
            agent_host = session_cls.host(host)
            self._harness_override = harness_host if isinstance(harness_host, Host) else None
            harness = (
                harness_host
                if isinstance(harness_host, Host)
                else session_cls.host(
                    harness_host
                    or config.harness_host(
                        self._settings, Host.parse_hosts(self._settings.get("hosts")).tables
                    )
                    or agent_host.name
                )
            )
            display = session_cls.display(display, host=agent_host.name)
        except (OSError, ValueError, SessionConfigError, DriverError) as error:
            raise ConfigError(str(error)) from None
        self.driver = driver or AgentDriver(
            session_cls, settings=self._settings, display=display, host=agent_host.name
        )
        run = self.composer.resolve(role, spec.task, overlays=overlays)
        accepted = plan(
            run,
            replace(spec, team=self.composer.team),
            driver=self.driver,
            composition=self.composer,
        )
        name = spec.name or f"{role}-{accepted.sid[:8]}"
        accepted = replace(
            accepted,
            spec=replace(accepted.spec, name=name if spec.mode == "tui" else None),
            session=replace(
                accepted.session,
                name=name if spec.mode == "tui" else None,
                protect=tuple(
                    dict.fromkeys((*spec.protect, *package_dirs(), *self.composer.team.dirs))
                ),
            ),
        )
        self.workdir = accepted.spec.workdir
        self._state = _State(
            role,
            accepted,
            f"{session_cls.__module__}:{session_cls.__qualname__}",
            agent_host.name,
            harness.name,
            display,
            None if callback is None else tuple(callback),
            name,
            f"{name}-drive",
            1,
            {},
            tuple(thaw(overlay) for overlay in overlays),
        )

    def _persist_new(self) -> None:
        try:
            os.makedirs(self.workdir, exist_ok=True)
            with file_lock(self._path, own=True):
                if read_regular(self._path, own=True) is not None:
                    previous = self._read()
                    if self._alive(previous):
                        raise ConfigError(f"{self.workdir}: delegate is still alive")
                    self._state = replace(self._state, attempt=previous.attempt + 1)
                self._save(self._state)
        except OSError as error:
            raise ConfigError(str(error)) from None

    @property
    def _harness_host(self) -> Host:
        return self._harness_override or self.session_cls.host(self._state.harness_host)

    @property
    def _path(self) -> str:
        return os.path.join(self.workdir, "delegate.json")

    def _configure(self) -> None:
        try:
            self.session_cls.configure_from(self._settings)
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _save(self, state: _State) -> None:
        team = state.plan.spec.team
        persisted = replace(
            state, plan=replace(state.plan, spec=replace(state.plan.spec, team=None))
        )
        values = {
            field.name: _encode(getattr(persisted, field.name)) for field in fields(persisted)
        }
        accepted = {"session": _encode(state.plan.session), "run": _encode(state.plan.run)}
        spec = {
            field.name: _encode(getattr(state.plan.spec, field.name))
            for field in fields(state.plan.spec)
            if field.name != "team"
        }
        spec["team"] = None if team is None else {"root": team.root, "dirs": team.dirs}
        values["plan"], values["spec"], values["version"] = accepted, spec, 1
        save(self._path, json.dumps(values), own=True, durable=True)

    def _read(self) -> _State:
        payload = read_regular(self._path, own=True)
        try:
            if payload is None:
                raise ValueError("missing metadata")
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("metadata must be an object")
            if data.pop("version") != 1:
                raise ValueError("unsupported metadata version")
            given = data.pop("spec")
            team = given.pop("team")
            given["protect"] = tuple(given["protect"])
            spec = Spec(
                **given, team=None if team is None else Team(team["root"], tuple(team["dirs"]))
            )
            if os.path.realpath(spec.workdir) != os.path.realpath(self.workdir):
                raise ValueError("metadata workdir differs")
            planned = data.pop("plan")
            session = planned["session"]
            access = session["access"]
            for key in ("dirs", "reads", "commands"):
                access[key] = tuple(access[key])
            session["access"] = Access(**access)
            session["protect"] = tuple(session["protect"])
            session["flags"] = tuple(session["flags"])
            session["reply_tags"] = tuple(session["reply_tags"])
            accepted = Plan(spec, SessionSpec(**session), RunConfig(**planned["run"]))
            result = data.pop("result")
            if result is not None:
                outcome = result.pop("outcome")
                if outcome is not None:
                    result["outcome"] = Outcome(**outcome)
                else:
                    result["outcome"] = None
                from_limit = result.pop("limit")
                if from_limit is not None:
                    result["limit"] = Limit(**from_limit)
                result = Result(**result)
            callback = data.pop("callback")
            overlays = tuple(data.pop("overlays"))
            state = _State(
                **data,
                plan=accepted,
                callback=None if callback is None else tuple(callback),
                result=result,
                overlays=overlays,
            )
            if type(state.attempt) is not int or state.attempt < 1:
                raise ValueError("invalid attempt")
        except (KeyError, TypeError, ValueError, SessionConfigError) as error:
            raise ConfigError(f"{self.workdir}: invalid delegate.json: {error}") from None
        else:
            return state

    @classmethod
    def attach(
        cls,
        record_or_workdir: Record | str,
        *,
        session_cls: type[Session] | None = None,
        composer: Composer | None = None,
    ) -> HarnessDelegate:
        workdir = (
            record_or_workdir.workdir
            if isinstance(record_or_workdir, Record)
            else record_or_workdir
        )
        if workdir is None:
            raise ConfigError("record has no workdir")
        view = cls.__new__(cls)
        view.workdir = os.path.abspath(workdir)
        view._state = view._read()
        try:
            view.composer = composer or Composer(
                config=DelegateConfig(config.read_config()),
                team=None
                if view._state.plan.spec.team is None
                else _team(view._state.plan.spec.team),
            )
            view._settings = thaw(view.composer.config.values)
            view.session_cls = session_cls or (
                Session
                if view._state.session_cls == f"{Session.__module__}:{Session.__qualname__}"
                else load_class(view._state.session_cls, Session)
            )
            view._configure()
            view.session_cls.host(view._state.host)
            view.session_cls.host(view._state.harness_host)
            view._harness_override = None
        except (OSError, ValueError, SessionConfigError, DriverError) as error:
            raise ConfigError(str(error)) from None
        view.driver = AgentDriver(
            view.session_cls,
            settings=view._settings,
            display=view._state.display,
            host=view._state.host,
        )
        return view

    @classmethod
    def roles(cls) -> dict[str, dict[str, str]]:
        return Composer().index()

    @classmethod
    def resolve(
        cls, role: str, task: str | None = None, *, overlays: Sequence[Mapping] = ()
    ) -> RunConfig:
        return Composer().resolve(role, task, overlays=overlays)

    @classmethod
    def trusted_dirs(cls) -> frozenset[str]:
        return Composer().trusted_dirs()

    @classmethod
    def usage(
        cls,
        role: str,
        *,
        overlays: Sequence[Mapping] = (),
        session_cls: type[Session] = Session,
    ) -> Usage | None:
        composition = Composer()
        try:
            session_cls.configure_from(thaw(composition.config.values))
            return session_cls.usage(composition.resolve(role, overlays=overlays).runtime)
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _agent(self, state: _State) -> Session | None:
        agent = self.driver.session
        if (
            agent is not None
            and state.attempt == self._state.attempt
            and agent.sid == state.plan.sid
        ):
            return agent
        try:
            return self.session_cls.attach(state.agent_name, host=state.host)
        except DriverError:
            return None
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _harness(self, state: _State) -> HostProcess | None:
        if not state.detached:
            return None
        try:
            host = (
                self._harness_override
                if self._harness_override is not None
                and self._harness_override.name == state.harness_host
                else self.session_cls.host(state.harness_host)
            )
            return host.attach(state.harness_name)
        except DriverError:
            return None
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _alive(self, state: _State) -> bool:
        if state.detached:
            process = self._harness(state)
            return process is not None and process.alive()
        if state.completed:
            return False
        if self._running():
            return True
        agent = self._agent(state)
        return agent is not None and agent.state() == Liveness.LIVE

    def _running(self) -> bool:
        try:
            descriptor = os.open(
                self._path + ".run.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except FileNotFoundError:
            return False
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                raise ConfigError(f"{self.workdir}: invalid delegate run lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(descriptor)

    def alive(self) -> bool:
        return self._alive(self._read())

    def outcome(self) -> Outcome | None:
        result = self._read().result
        return None if result is None else result.outcome

    def _record(self, state: _State) -> Record:
        sessions = (state.harness_name,) if state.detached else ()
        if state.plan.spec.mode == "tui":
            sessions += (state.agent_name,)
        return Record(
            1,
            state.outer.get("kind", state.role),
            state.agent_name,
            sessions,
            state.plan.sid,
            self.workdir,
            tuple(state.outer.get("ctl", ("agent-pm-delegate",))),
            tuple(state.outer.get("resume", ("agent-pm-delegate", "resume", self.workdir))),
            state.display,
            state.role,
        )

    def status(self) -> Status:
        state = self._read()
        agent = self._agent(state)
        word = (
            str(agent.state())
            if agent is not None
            else ("live" if self._alive(state) else "absent")
        )
        return Status(
            state.agent_name,
            self._alive(state),
            word,
            None
            if state.result is None or state.result.outcome is None
            else state.result.outcome.status,
            self.attach_command(),
        )

    def attach_command(self) -> str | None:
        state = self._read()
        agent = self._agent(state)
        if agent is not None and agent.state() == Liveness.LIVE and agent.displayable:
            return agent.attach_command()
        process = self._harness(state)
        if process is not None and process.alive() and isinstance(self._harness_host, Displayable):
            return self._harness_host.attach_command(state.harness_name)
        return None

    def show(self) -> str | None:
        state = self._read()
        commands = []
        capable = False
        agent = self._agent(state)
        if agent is not None and agent.state() == Liveness.LIVE and agent.displayable:
            capable = True
            if command := agent.show(display=state.display):
                commands.append(command)
        process = self._harness(state)
        if process is not None and process.alive() and isinstance(self._harness_host, Displayable):
            capable = True
            if command := self._harness_host.show(state.harness_name, display=state.display):
                commands.append(command)
        if not capable:
            raise NotDisplayable("delegate has no live displayable session")
        return "\n".join(commands) or None

    def close(self) -> None:
        state = self._read()
        agent = self._agent(state)
        if agent is not None:
            agent.close()
        process = self._harness(state)
        if process is not None:
            try:
                process.close()
            except DriverError as error:
                raise DriverError(f"close {state.harness_name}: {error}") from error

    def session_info(self) -> SessionStatus:
        state = self._read()
        if state.completed:
            return self.session_cls.inspect(state.plan.session)
        agent = self._agent(state)
        return (
            agent.session_status()
            if agent is not None
            else self.session_cls.inspect(state.plan.session)
        )

    def run(self, on_event: Callable[[Event], None] | None = None) -> Result:
        with file_lock(self._path + ".run", own=True):
            with file_lock(self._path, own=True):
                if self._current().completed:
                    raise ConfigError(f"{self.workdir}: delegate run already completed")
            callback = (
                None
                if self._state.callback is None
                else EventCallback(self._state.callback, self.workdir)
            )

            def emit(event: Event) -> None:
                if isinstance(event, Finished):
                    with file_lock(self._path, own=True):
                        state = self._current()
                        self._state = replace(state, result=event.result, completed=True)
                        self._save(self._state)
                if on_event is not None:
                    on_event(event)
                if callback is not None:
                    callback(event)

            return execute(self._state.plan, driver=self.driver, on_event=emit)

    def _current(self) -> _State:
        state = self._read()
        if (state.plan.sid, state.attempt) != (self._state.plan.sid, self._state.attempt):
            raise ConfigError(f"{self.workdir}: delegate attempt was replaced")
        return state

    def _detach(self, state: _State) -> Record:
        state = replace(state, detached=True)
        self._save(state)
        record = self._record(state)
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        environment.update(import_env(config, session_package, utils_package))
        self._harness_host.detach(
            state.harness_name,
            Launch(
                (
                    sys.executable,
                    "-P",
                    "-m",
                    "agent_pm.delegate.engine.harness_delegate",
                    self.workdir,
                ),
                cwd=state.plan.session.access.cwd or self.workdir,
                env=environment,
                blocked_signals=SIGNALS,
            ),
            display=state.display,
            events=state.plan.spec.events,
            record=record,
        )
        self._state = state
        return record

    def detach(self, *, outer: Outer | None = None) -> Record:
        with file_lock(self._path, own=True):
            state = self._current()
            if self._alive(state):
                return self._record(state)
            if state.completed:
                raise ConfigError(f"{self.workdir}: delegate run already completed")
            state = replace(state, outer={} if outer is None else outer.copy())
            return self._detach(state)

    def resume(self) -> Record:
        with file_lock(self._path, own=True):
            state = self._current()
            if self._alive(state) or state.result is not None and state.result.outcome is not None:
                return self._record(state)
            agent = self._agent(state)
            if agent is not None:
                agent.close()
            original = state.plan.session.access
            if (
                original.project
                and under(os.path.realpath(original.cwd), self.composer.trusted_dirs()) is None
            ):
                raise ConfigError(f"cwd {original.cwd} is no longer in trusted_dirs")
            spec = replace(state.plan.spec, resume=True, cwd=original.cwd, team=self.composer.team)
            accepted = plan(
                self.composer.resolve(state.role, spec.task, overlays=state.overlays),
                spec,
                driver=self.driver,
                composition=self.composer,
            )
            accepted = replace(
                accepted,
                session=replace(
                    accepted.session,
                    access=replace(
                        accepted.session.access, cwd=original.cwd, project=original.project
                    ),
                    protect=tuple(
                        dict.fromkeys(
                            (
                                *spec.protect,
                                *package_dirs(),
                                *self.composer.team.dirs,
                            )
                        )
                    ),
                ),
            )
            try:
                self.driver.preflight(accepted.session)
            except SessionConfigError as error:
                raise ConfigError(str(error)) from None
            state = replace(state, plan=accepted, completed=False, result=None)
            return self._detach(state)


def _stop(signum, _frame):
    signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    raise Stopped(128 + signum)


def main(argv: list[str]) -> int:
    owner = HarnessDelegate.attach(argv[0])
    for signum in SIGNALS:
        signal.signal(signum, _stop)
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
        return owner.run().returncode
    except Stopped as stopped:
        return stopped.code
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
