"""One run's plan: its cwd, Spec, sid and prompt, every check before anything starts, writing nothing. A refusal is
ConfigError, a Composer or Driver one keeping its message."""

import functools
import os
import sys
import uuid
from dataclasses import dataclass, replace

from agent_pm import composer
from agent_pm.composer import RunConfig, RunParams, Team
from agent_pm.driver import api
from agent_pm.utils.paths import under

DELIVERABLE = os.path.join("tmp", "deliverable.md")  # under the workdir, without `out`


class ConfigError(Exception):
    pass


def _own(f):
    @functools.wraps(f)
    def wrapped(*args, **kw):
        try:
            return f(*args, **kw)
        except (composer.ConfigError, api.ConfigError) as e:
            raise ConfigError(str(e)) from None

    return wrapped


@dataclass(frozen=True)
class Plan:
    """`out` None: nothing is saved."""

    runtime: str
    mode: str
    sid: str
    resume: bool
    prompt: str
    spec: api.Spec
    run: RunConfig
    params: RunParams
    out: str | None
    name: str | None
    events: str | None


@_own
def place(
    run: RunConfig, workdir: str, *, resume_sid: str | None, cwd: str | None
) -> tuple[str, bool]:
    """(cwd, whether its project settings load); a resume's from its session record."""
    workdir, trusted = os.path.abspath(workdir), composer.trusted_dirs()
    if resume_sid is not None:
        # The agent run can append to run.jsonl: this re-check limits a forged record.
        entry = api.session(workdir, resume_sid)
        here, project = (entry["cwd"], entry["project"]) if entry else (workdir, False)
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
    if not project and real != real_work:
        print(
            f"delegate: cwd {here} is not in trusted_dirs: its project settings are off",
            file=sys.stderr,
        )
    return here, project


@_own
def plan(
    run: RunConfig,
    task: str | None,
    input: str,
    workdir: str,
    *,
    runtime: str,
    mode: str,
    sid: str | None = None,
    resume: str | None = None,
    name: str | None = None,
    events: str | None = None,
    out: str | None = None,
    repo: str | None = None,
    cwd: str | None = None,
    team: Team | None = None,
) -> Plan:
    """`team` None: the built-in team, the configured team dirs checked for overlap only."""
    if mode not in api.MODES:
        raise ConfigError(f"mode must be one of {', '.join(api.MODES)}, got {mode!r}")
    if mode != "tui" and name is not None:
        raise ConfigError("name: tui mode only")
    if mode != "tui" and events is not None:
        raise ConfigError("events: tui mode only")
    if sid is not None and resume is not None and sid != resume:
        raise ConfigError(f"sid {sid!r:.80} differs from resume {resume!r:.80}")
    guarded: tuple[str, ...] = ()
    if team is None:
        team = composer.team()
        try:
            guarded = composer.team_dirs()
        except (composer.ConfigError, OSError, ValueError):
            pass
    tasks = composer.index(run.role, team)
    if task:
        if task not in tasks:
            raise ConfigError(
                f"task {task!r} is not one of {run.role}'s tasks ({', '.join(tasks)})"
            )
        run = replace(run, task=task)
    sid = resume if resume is not None else sid if sid is not None else str(uuid.uuid4())
    workdir, out = os.path.abspath(workdir), os.path.abspath(out) if out else None
    here, project = place(run, workdir, resume_sid=resume, cwd=cwd)
    intent = composer.access(run, workdir, cwd=here, project=project, repo_dir=repo, team=team)
    access = api.Access(
        dirs=intent.dirs,
        commands=intent.commands + ((intent.gate,) if intent.gate else ()),
        cwd=here,
        project=project,
        reads=intent.reads,
        env=intent.env,
    )
    spec = api.Spec(workdir=workdir, access=access, tier=run.tier, effort=run.effort)
    _check_team_dirs(team, guarded, intent.writes, runtime=runtime, spec=spec, resume=resume)
    params = RunParams(
        input=input,
        out=out or os.path.join(workdir, DELIVERABLE),
        workdir=workdir,
        resume=resume is not None,
    )
    return Plan(
        runtime,
        mode,
        sid,
        resume is not None,
        composer.render(run, params, team=team),
        spec,
        run,
        params,
        out,
        name,
        events,
    )


def _check_team_dirs(
    team: Team,
    guarded: tuple[str, ...],
    write_dirs: tuple[str, ...],
    *,
    runtime: str,
    spec: api.Spec,
    resume: str | None,
) -> None:
    """Every dir the run can write counts, those `runtime` can write itself (api.writable_dirs) last; spec.access.reads,
    read only, do not."""
    if not team.dirs and not guarded:
        return
    writes = [("the workdir", spec.workdir), ("the cwd", spec.access.cwd)]
    writes += [(f"write dir {p}", p) for p in write_dirs]
    writes += [("a dir the run adds", p) for p in spec.access.dirs]
    writes += [(f"a dir {runtime} can write", p) for p in api.writable_dirs(runtime, spec, resume)]
    composer.check_team_dirs(team.dirs, writes=writes)
    for path in (p for d in guarded for p in composer.team_paths(d)):
        real = os.path.realpath(path)
        composer.check_overlap(path if real == path else f"{path} ({real})", real, writes)
