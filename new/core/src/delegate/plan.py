"""One run's plan, every check before anything starts: its cwd (place), the Driver's Spec with the Access built from
the Composer's Intent, its sid and its prompt (plan). Writes and creates nothing; a refusal is the Delegate's
ConfigError, a Composer or Driver one keeping its message. Ported from the old core's driver (place, plan)."""
import functools
import os
import sys
import uuid
from dataclasses import dataclass, replace

import composer
from composer import RunConfig, RunParams, Team
from driver import api
from utils.paths import under

DELIVERABLE = os.path.join("tmp", "deliverable.md")   # in the workdir: the prompt's deliverable without `out`


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
    """`sid`: a resume's, else the caller's, else a new uuid4; `out` absolute, None: nothing is saved."""
    runtime: str
    mode: str
    sid: str
    resume: bool
    prompt: str
    spec: api.Spec
    run: RunConfig
    params: RunParams
    out: str | None


@_own
def place(run: RunConfig, workdir: str, *, resume_sid: str | None, cwd: str | None) -> tuple[str, bool]:
    """(the run's cwd, whether its project settings load), as the core config's `cwd` and `trusted_dirs` say. A new
    run: `run.cwd`, else `cwd`, else the current directory; a resume: its session record's (driver.api.session), none:
    the workdir, project off."""
    workdir, trusted = os.path.abspath(workdir), composer.trusted_dirs()
    if resume_sid is not None:
        # The agent run can append to run.jsonl: only the trusted_dirs re-check limits a forged record.
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
        raise ConfigError(f"cwd {here} is in trusted_dirs and in the workdir: project settings must not load from it")
    if not project and real != real_work:
        print(f"delegate: cwd {here} is not in trusted_dirs: its project settings are off", file=sys.stderr)
    return here, project


@_own
def plan(run: RunConfig, task: str | None, input: str, workdir: str, *, runtime: str, mode: str,
         sid: str | None = None, resume: str | None = None, name: str | None = None, events: str | None = None,
         out: str | None = None, repo: str | None = None, cwd: str | None = None, team: Team | None = None) -> Plan:
    """The Plan of `run` on `task` (None: the run's own) and `input`; `resume` a session id to resume. `team` None: the
    built-in team, the configured team dirs checked for overlap only (_check_team_dirs)."""
    if mode not in api.MODES:
        raise ConfigError(f"mode must be headless or tui, got {mode!r}")
    if mode == "tui":
        raise ConfigError("tui mode is not available yet")
    if name is not None:
        raise ConfigError("name: tui mode only")
    if events is not None:
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
            raise ConfigError(f"task {task!r} is not one of {run.role}'s tasks ({', '.join(tasks)})")
        run = replace(run, task=task)
    sid = resume if resume is not None else sid if sid is not None else str(uuid.uuid4())
    workdir, out = os.path.abspath(workdir), os.path.abspath(out) if out else None
    here, project = place(run, workdir, resume_sid=resume, cwd=cwd)
    intent = composer.access(run, workdir, cwd=here, project=project, repo_dir=repo, team=team)
    access = api.Access(intent.dirs, intent.commands + ((intent.gate,) if intent.gate else ()), here, project)
    spec = api.Spec(workdir=workdir, access=access, tier=run.tier, effort=run.effort)
    _check_team_dirs(run, team, guarded, workdir, here, intent.writes, intent.dirs)
    params = RunParams(input=input, out=out or os.path.join(workdir, DELIVERABLE), workdir=workdir,
                       resume=resume is not None)
    return Plan(runtime, mode, sid, resume is not None, composer.render(run, params, team=team), spec, run, params, out)


def _check_team_dirs(run: RunConfig, team: Team, guarded: tuple[str, ...], workdir: str, here: str,
                     write_dirs: tuple[str, ...], dirs: tuple[str, ...]) -> None:
    """`team`'s dirs checked whole (composer.check_team_dirs), the `guarded` ones for overlap only, against what the run
    can write: the workdir, the cwd, each write dir and each added dir but the role's own tasks dir. Dirs an Adapter
    adds itself are its own to guard: driver.api exposes none."""
    if not team.dirs and not guarded:
        return
    writes = [("the workdir", workdir), ("the cwd", here)] + [(f"write dir {p}", p) for p in write_dirs]
    own = team.tasks_dir(run.role)
    writes += [("a dir the run adds", p) for p in dirs if not own or os.path.realpath(p) != os.path.realpath(own)]
    composer.check_team_dirs(team.dirs, writes=writes)
    for path in (p for d in guarded for p in composer.team_paths(d)):
        real = os.path.realpath(path)
        composer.check_overlap(path if real == path else f"{path} ({real})", real, writes)
