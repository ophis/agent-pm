"""One run's plan: its cwd, session Spec, sid and prompt, every check first, writing nothing; the
caller configured the session (configure). A refusal is ConfigError, a Driver one keeping its
message.
"""

import contextlib
import functools
import os
import sys
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import replace

from agent_pm.delegate import composer
from agent_pm.delegate.composer import DELIVERABLE, RunConfig, RunParams, Team
from agent_pm.delegate.engine import config
from agent_pm.delegate.engine.types import ConfigError, Plan, Spec
from agent_pm.delegate.event.reply import OUTCOME, PROGRESS
from agent_pm.session import MODES, Access, Driver, parse_hosts, session_record
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import LOCAL, read_project, under


def configure(*, local: str = LOCAL, cfg: Mapping | None = None) -> Driver:
    """The session from its config once harness_host (`cfg`, else read) names its host."""
    with _config("session"):
        table = read_project("session", local=local)
        hosts = parse_hosts(table.get("hosts")).tables
    with _config("delegate"):
        config.harness_host(read_project("delegate", local=local) if cfg is None else cfg, hosts)
    with _config("session"):
        return Driver.from_config(table)


@contextlib.contextmanager
def _config(name: str) -> Iterator[None]:
    try:
        yield
    except (OSError, ValueError, SessionConfigError) as e:
        raise ConfigError.layer_configuration(name, e) from None


def policy(runtime: str) -> tuple[tuple[str, ...], dict[str, str]]:
    try:
        flags, env = config.policy(runtime)
        spec = SessionSpec(workdir="/", flags=tuple(flags), env=env)
        return spec.flags, dict(spec.env)
    except (OSError, ValueError, SessionConfigError) as exc:
        raise ConfigError.policy(runtime, exc) from None


def _own(f):
    @functools.wraps(f)
    def wrapped(*args, **kw):
        try:
            return f(*args, **kw)
        except SessionConfigError as e:
            raise ConfigError(str(e)) from None

    return wrapped


@_own
def place(
    run: RunConfig, workdir: str, *, resume_sid: str | None, cwd: str | None
) -> tuple[str, bool]:
    """(cwd, whether its project settings load); a resume's from its session record."""
    workdir, trusted = os.path.abspath(workdir), composer.trusted_dirs()
    if resume_sid is not None:
        # The agent run can append to run.jsonl: this re-check limits a forged record.
        entry = session_record(workdir, resume_sid)
        here, project = (entry["cwd"], entry["project"]) if entry else (workdir, False)
        if project and not under(os.path.realpath(here), trusted):
            raise ConfigError.untrusted_resume_cwd(here)
    else:
        here = os.path.abspath(os.path.expanduser(run.cwd or cwd or os.getcwd()))
        project = under(os.path.realpath(here), trusted) is not None
    real, real_work = os.path.realpath(here), os.path.realpath(workdir)
    if real != real_work and not os.path.isdir(here):
        raise ConfigError.cwd_missing(here)
    if project and under(real, [real_work]):
        raise ConfigError.cwd_inside_workdir(here)
    if not project and real != real_work:
        print(
            f"delegate: cwd {here} is not in trusted_dirs: its project settings are off",
            file=sys.stderr,
        )
    return here, project


@_own
def plan(run: RunConfig, spec: Spec, *, driver: Driver | None = None) -> Plan:
    """With the built-in team the configured team dirs are checked for overlap only."""
    driver = configure() if driver is None else driver
    if spec.mode not in MODES:
        raise ConfigError.mode(MODES, spec.mode)
    if spec.mode != "tui" and spec.name is not None:
        raise ConfigError.name_mode()
    if spec.mode != "tui" and spec.events is not None:
        raise ConfigError.events_mode()
    if spec.resume and spec.sid is None:
        raise ConfigError.resume_sid()
    team, guarded = spec.team, ()
    if team is None:
        team = composer.team()
        try:
            guarded = composer.team_dirs()
        except (ConfigError, OSError, ValueError):
            pass
    tasks = composer.index(run.role, team)
    if spec.task:
        if spec.task not in tasks:
            raise ConfigError.task(spec.task, run.role, tasks)
        run = replace(run, task=spec.task)
    workdir = os.path.abspath(spec.workdir)
    spec = replace(
        spec,
        workdir=workdir,
        out=os.path.abspath(spec.out) if spec.out else None,
        runtime=spec.runtime or run.runtime,
        sid=spec.sid if spec.sid is not None else str(uuid.uuid4()),
        team=team,
    )
    resume = spec.sid if spec.resume else None
    here, project = place(run, workdir, resume_sid=resume, cwd=spec.cwd)
    intent = composer.access(run, workdir, cwd=here, project=project, repo_dir=spec.repo, team=team)
    access = Access(
        dirs=intent.dirs,
        commands=intent.commands + ((intent.gate,) if intent.gate else ()),
        cwd=here,
        project=project,
        reads=intent.reads,
        env=intent.env,
    )
    flags, env = policy(spec.runtime)
    session = SessionSpec(
        workdir=workdir,
        access=access,
        tier=run.tier,
        effort=run.effort,
        flags=flags,
        env=env,
        reply_tags=(PROGRESS, OUTCOME),
    )
    _check_team_dirs(
        team,
        guarded,
        intent.writes,
        runtime=spec.runtime,
        spec=session,
        resume=resume,
        driver=driver,
    )
    params = RunParams(
        input=spec.input,
        out=spec.out or os.path.join(workdir, DELIVERABLE),
        workdir=workdir,
        resume=spec.resume,
    )
    return Plan(spec, session, composer.render(run, params, team=team), run, params, driver)


def _check_team_dirs(
    team: Team,
    guarded: tuple[str, ...],
    write_dirs: tuple[str, ...],
    *,
    runtime: str,
    spec: SessionSpec,
    resume: str | None,
    driver: Driver,
) -> None:
    """Every dir the run can write counts, those `runtime` can write itself (writable_dirs) last;
    spec.access.reads, read only, do not.
    """
    if not team.dirs and not guarded:
        return
    writes = [("the workdir", spec.workdir), ("the cwd", spec.access.cwd)]
    writes += [(f"write dir {p}", p) for p in write_dirs]
    writes += [("a dir the run adds", p) for p in spec.access.dirs]
    writes += [
        (f"a dir {runtime} can write", p) for p in driver.writable_dirs(runtime, spec, resume)
    ]
    composer.check_team_dirs(team.dirs, writes=writes)
    for path in (p for d in guarded for p in composer.team_paths(d)):
        real = os.path.realpath(path)
        composer.check_overlap(path if real == path else f"{path} ({real})", real, writes)
