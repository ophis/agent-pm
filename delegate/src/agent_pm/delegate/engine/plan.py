"""One run's plan: its cwd, session Spec, sid and prompt, every check first, writing nothing. The
caller supplies its Driver. A refusal is ConfigError, a Driver one keeping its message.
"""

import contextlib
import functools
import os
import sys
import uuid
from dataclasses import replace

from agent_pm.delegate import composer
from agent_pm.delegate.composer import DELIVERABLE, Composer, RunConfig, RunParams, Team
from agent_pm.delegate.engine.drivers import Driver
from agent_pm.delegate.engine.types import ConfigError, Plan, Spec
from agent_pm.delegate.event.reply import OUTCOME, PROGRESS
from agent_pm.session import Access, Mode, RunLog
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import under


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
    run: RunConfig,
    workdir: str,
    *,
    resume_sid: str | None,
    cwd: str | None,
    trusted: frozenset[str] | None = None,
) -> tuple[str, bool]:
    """(cwd, whether its project settings load); a resume's from its session record."""
    workdir = os.path.abspath(workdir)
    trusted = composer.trusted_dirs() if trusted is None else trusted
    if resume_sid is not None:
        # The agent run can append to run.jsonl: this re-check limits a forged record.
        entry = RunLog(workdir).session(resume_sid)
        here, project = (entry.cwd, entry.project) if entry else (workdir, False)
        if project and not under(os.path.realpath(here), trusted):
            raise ConfigError(f"cwd {here} is no longer in trusted_dirs")
    else:
        here = os.path.abspath(os.path.expanduser(run.cwd or cwd or os.getcwd()))
        project = under(os.path.realpath(here), trusted) is not None
    real, real_work = os.path.realpath(here), os.path.realpath(workdir)
    if real != real_work and not os.path.isdir(here):
        raise ConfigError(f"cwd {here} is not a directory")
    if project and under(real, [real_work]):
        raise ConfigError(
            f"cwd {here} is in trusted_dirs and in the workdir: project settings must not load from it"
        )
    return here, project


@_own
def plan(
    run: RunConfig,
    spec: Spec,
    *,
    driver: Driver,
    composition: Composer | None = None,
) -> Plan:
    """With the built-in team the configured team dirs are checked for overlap only."""
    if not driver.session_based and spec.resume:
        raise ConfigError("instruction has no session to resume")
    if spec.mode not in tuple(Mode):
        raise ConfigError(f"mode must be one of {', '.join(Mode)}, got {spec.mode!r}")
    if spec.mode != "tui" and spec.name is not None:
        raise ConfigError("name: tui mode only")
    if spec.mode != "tui" and spec.events is not None:
        raise ConfigError("events: tui mode only")
    if spec.resume and spec.sid is None:
        raise ConfigError("resume: needs a sid")
    team, guarded = spec.team, ()
    if team is None:
        team = composer.team()
        with contextlib.suppress(ConfigError, OSError, ValueError):
            guarded = composer.team_dirs()
    tasks = composer.index(run.role, team) if composition is None else composition.index(run.role)
    if spec.task:
        if spec.task not in tasks:
            raise ConfigError(
                f"task {spec.task!r} is not one of {run.role}'s tasks ({', '.join(tasks)})"
            )
        run = replace(run, task=spec.task)
    workdir = os.path.abspath(spec.workdir)
    runtime = spec.runtime or run.runtime
    spec = replace(
        spec,
        workdir=workdir,
        out=os.path.abspath(spec.out) if spec.out else None,
        runtime=runtime,
        sid=spec.sid if spec.sid is not None else str(uuid.uuid4()),
        team=team,
    )
    resume = spec.sid if spec.resume else None
    here, project = place(
        run,
        workdir,
        resume_sid=resume,
        cwd=spec.cwd,
        trusted=None if composition is None else composition.trusted_dirs(),
    )
    if driver.session_based and not project and os.path.realpath(here) != os.path.realpath(workdir):
        print(
            f"delegate: cwd {here} is not in trusted_dirs: its project settings are off",
            file=sys.stderr,
        )
    intent = composer.access(run, workdir, cwd=here, project=project, repo_dir=spec.repo, team=team)
    access = Access(
        dirs=intent.dirs,
        commands=intent.commands + ((intent.gate,) if intent.gate else ()),
        cwd=here,
        project=project,
        reads=intent.reads,
        env=intent.env,
    )
    flags, env = driver.policy(runtime)
    session = SessionSpec(
        workdir=workdir,
        runtime=runtime,
        mode=Mode(spec.mode),
        sid=spec.sid,
        resume=spec.resume,
        name=spec.name,
        events=spec.events,
        access=access,
        tier=run.tier,
        effort=run.effort,
        flags=flags,
        env=env,
        reply_tags=(PROGRESS, OUTCOME),
        protect=spec.protect,
    )
    _check_team_dirs(
        team,
        guarded,
        intent.writes,
        runtime=runtime,
        spec=session,
        driver=driver,
    )
    params = RunParams(
        input=spec.input,
        out=spec.out or os.path.join(workdir, DELIVERABLE),
        workdir=workdir,
        resume=spec.resume,
    )
    prompt = (
        composer.render(run, params, team=team)
        if composition is None
        else composition.render(run, params)
    )
    return Plan(spec, replace(session, prompt=prompt), run)


def _check_team_dirs(
    team: Team,
    guarded: tuple[str, ...],
    write_dirs: tuple[str, ...],
    *,
    runtime: str,
    spec: SessionSpec,
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
    writes += [(f"a dir {runtime} can write", p) for p in driver.writable_dirs(spec)]
    composer.check_team_dirs(team.dirs, writes=writes)
    for path in (p for d in guarded for p in composer.team_paths(d)):
        real = os.path.realpath(path)
        composer.check_overlap(path if real == path else f"{path} ({real})", real, writes)
