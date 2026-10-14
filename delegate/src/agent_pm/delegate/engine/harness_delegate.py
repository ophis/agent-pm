"""HarnessDelegate owns an accepted plan, its Composer, sessions and durable recovery identity."""

from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace

import agent_pm.utils as utils_package
from agent_pm.delegate import config
from agent_pm.delegate.composer import Composer, DelegateConfig, RunConfig, Team, teams
from agent_pm.delegate.engine.base import Delegate
from agent_pm.delegate.engine.callback import EventCallback
from agent_pm.delegate.engine.config import module_roots, package_dirs
from agent_pm.delegate.engine.drivers import AgentDriver, Driver, InstructionDriver
from agent_pm.delegate.engine.plan import plan
from agent_pm.delegate.engine.runner import execute
from agent_pm.delegate.engine.state import RunState
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
    Mode,
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
    load_class,
    source_path,
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
    detached: bool = False
    completed: bool = False
    result: Result | None = None
    events: str | None = None


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
            hosts = Host.parse_hosts(self._settings.get("hosts")).tables
            agent_host = session_cls.host(host)
            self._harness_override = harness_host if isinstance(harness_host, Host) else None
            harness = (
                harness_host
                if isinstance(harness_host, Host)
                else session_cls.host(
                    harness_host or config.harness_host(self._settings, hosts) or agent_host.name
                )
            )
            display = session_cls.display(display, host=agent_host.name)
            events = harness.events_path(spec.events)
        except (OSError, ValueError, SessionConfigError, DriverError) as error:
            raise ConfigError(str(error)) from None
        if harness.name not in hosts:
            raise ConfigError(
                f"harness_host {harness.name}: want one of {', '.join(sorted(hosts))}"
            )
        self.driver = driver or AgentDriver(
            session_cls, settings=self._settings, display=display, host=agent_host.name
        )
        tui = spec.mode == Mode.TUI
        run = self.composer.resolve(role, spec.task)
        accepted = plan(
            run,
            replace(
                spec,
                team=self.composer.team,
                name=spec.name if tui else None,
                events=events if tui else None,
            ),
            driver=self.driver,
            composition=self.composer,
        )
        name = spec.name or f"{role}-{accepted.sid[:8]}"
        accepted = replace(
            accepted,
            spec=replace(accepted.spec, name=name if tui else None),
            session=replace(
                accepted.session,
                name=name if tui else None,
                protect=tuple(
                    dict.fromkeys((*spec.protect, *package_dirs(), *self.composer.team.dirs))
                ),
            ),
        )
        self.workdir = accepted.spec.workdir
        self.store = RunState(self.workdir)
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
            events=events,
        )
        try:
            harness.validate(
                self._state.harness_name, self._harness_launch(self._state), display=display
            )
        except (SessionConfigError, DriverError) as error:
            raise ConfigError(str(error)) from None

    def _persist_new(self) -> None:
        try:
            with self.store.mutex():
                payload = self.store.read()
                if payload is not None:
                    self._state = replace(self._state, attempt=self._replacing(payload))
                self._save(self._state)
        except OSError as error:
            raise ConfigError(str(error)) from None

    def _replacing(self, payload: bytes) -> int:
        """The attempt replacing the run `payload` records, refused while that run is alive."""
        try:
            previous = self._parse(payload)
        except ConfigError:
            return self._salvaged(payload)
        if self._alive(previous):
            raise ConfigError(f"{self.workdir}: delegate is still alive")
        return previous.attempt + 1

    def _salvaged(self, payload: bytes) -> int:
        """A corrupt record's run counts as alive while its run lock is held or the harness it
        names lives; its attempt is kept when readable.
        """
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        data = data if isinstance(data, dict) else {}
        host, name = data.get("harness_host"), data.get("harness_name")
        if self.store.running() or (
            isinstance(host, str)
            and isinstance(name, str)
            and (process := self._process(host, name)) is not None
            and process.alive()
        ):
            raise ConfigError(f"{self.workdir}: delegate is still alive")
        attempt = data.get("attempt")
        return attempt + 1 if type(attempt) is int and attempt >= 1 else 1

    @property
    def _harness_host(self) -> Host:
        return self._harness_override or self.session_cls.host(self._state.harness_host)

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
        self.store.write(json.dumps(values))

    def _read(self) -> _State:
        return self._parse(self.store.read())

    def _parse(self, payload: bytes | None) -> _State:
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
            state = _State(
                **data,
                plan=accepted,
                callback=None if callback is None else tuple(callback),
                result=result,
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
        view.store = RunState(view.workdir)
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
    def usage(cls, role: str, *, session_cls: type[Session] = Session) -> Usage | None:
        composition = Composer()
        try:
            session_cls.configure_from(thaw(composition.config.values))
            return session_cls.usage(composition.resolve(role).runtime)
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
        return self._process(state.harness_host, state.harness_name) if state.detached else None

    def _process(self, host_name: str, name: str) -> HostProcess | None:
        try:
            host = (
                self._harness_override
                if self._harness_override is not None and self._harness_override.name == host_name
                else self.session_cls.host(host_name)
            )
            return host.attach(name)
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
        if self.store.running():
            return True
        agent = self._agent(state)
        return agent is not None and agent.state() == Liveness.LIVE

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
            state.outer.get("kind", "role"),
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
        state = self._state
        if state.events is not None and not state.detached and state.plan.spec.mode != Mode.TUI:
            raise ConfigError("events: a headless run writes them only when detached")
        with self.store.run_lock():
            with self.store.mutex():
                if self._current().completed:
                    raise ConfigError(f"{self.workdir}: delegate run already completed")
            callback = (
                None
                if self._state.callback is None
                else EventCallback(
                    self._state.callback,
                    self.workdir,
                    sid=self._state.plan.sid,
                    attempt=self._state.attempt,
                )
            )

            def emit(event: Event) -> None:
                if isinstance(event, Finished):
                    with self.store.mutex():
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

    def _harness_launch(self, state: _State) -> Launch:
        """The harness imports from the delegate, session and utils roots, then from its session
        class's and the classes that session loads by name for the run's runtime.
        """
        try:
            roots = (
                *module_roots(),
                *utils_package.module_roots(sys.modules[self.session_cls.__module__]),
                *self.session_cls.class_roots(state.plan.runtime),
            )
        except (ValueError, SessionConfigError) as error:
            raise ConfigError(str(error)) from None
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        environment.update(source_path(roots))
        return Launch(
            (sys.executable, "-P", "-m", "agent_pm.delegate.engine.harness_delegate", self.workdir),
            cwd=state.plan.session.access.cwd or self.workdir,
            env=environment,
            blocked_signals=SIGNALS,
        )

    def _launch(self, previous: _State, state: _State) -> Record:
        """`state` saved detached before its harness starts; `previous` again when it cannot."""
        state = replace(state, detached=True)
        record = self._record(state)
        launch = self._harness_launch(state)
        self._save(state)
        try:
            self._harness_host.detach(
                state.harness_name,
                launch,
                display=state.display,
                events=state.events,
                record=record,
            )
        except BaseException as error:
            self._save(previous)
            if isinstance(error, SessionConfigError):
                raise ConfigError(str(error)) from None
            raise
        self._state = state
        return record

    def detach(self, *, outer: Outer | None = None) -> Record:
        mode = str(self._state.plan.spec.mode)
        if mode not in (Mode.HEADLESS, Mode.TUI):
            raise ConfigError(f"detach: mode must be headless or tui, got {mode!r}")
        if not self.driver.session_based:
            raise ConfigError("instruction cannot detach")
        given: Outer = {} if outer is None else outer.copy()
        if unknown := next((key for key in given if key not in Outer.__annotations__), None):
            raise ConfigError(f"outer: unknown key {unknown}")
        try:
            self._record(replace(self._state, outer=given))
        except ValueError as error:
            raise ConfigError(f"outer: {error}") from None
        with self.store.mutex():
            previous = self._current()
            if self._alive(previous):
                return self._record(previous)
            if previous.completed:
                raise ConfigError(f"{self.workdir}: delegate run already completed")
            return self._launch(previous, replace(previous, outer=given))

    def resume(self) -> Record:
        with self.store.mutex():
            state = previous = self._state = self._read()
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
                self.composer.resolve(state.role, spec.task),
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
            state = replace(
                state, plan=accepted, completed=False, result=None, attempt=state.attempt + 1
            )
            return self._launch(previous, state)


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
