"""One run's plan: its cwd, session Spec, sid and prompt, every check first, writing nothing. The
caller supplies its Driver and its Composer, whose team (not spec.team), trusted_dirs, access and
prompt the run takes. A refusal is ConfigError, a Driver one keeping its message.
"""

import functools
import os
import sys
import uuid
from dataclasses import replace

from agent_pm.delegate.composer import Composer, RunConfig
from agent_pm.delegate.engine.drivers import Driver
from agent_pm.delegate.engine.types import ConfigError, Layout, Plan, Spec
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
    layout: Layout,
    *,
    resume_sid: str | None,
    cwd: str | None,
    trusted: frozenset[str],
) -> tuple[str, bool]:
    """(cwd, whether its project settings load); a resume's from its session record."""
    if resume_sid is not None:
        # The agent run can append to run.jsonl: this re-check limits a forged record.
        entry = RunLog(layout.run).session(resume_sid)
        here, project = (entry.cwd, entry.project) if entry else (layout.agent, False)
        if project and not under(os.path.realpath(here), trusted):
            raise ConfigError(f"cwd {here} is no longer in trusted_dirs")
    else:
        here = os.path.abspath(os.path.expanduser(run.cwd or cwd or layout.agent))
        project = under(os.path.realpath(here), trusted) is not None
    _refuse_overlap("cwd", here, layout)
    real = os.path.realpath(here)
    if real != os.path.realpath(layout.agent) and not os.path.isdir(here):
        raise ConfigError(f"cwd {here} is not a directory")
    if project and under(real, [os.path.realpath(layout.workdir)]):
        raise ConfigError(
            f"cwd {here} is in trusted_dirs and in the workdir: project settings must not load from it"
        )
    return here, project


def _refuse_overlap(label: str, path: str, layout: Layout) -> None:
    real, run = os.path.realpath(path), os.path.realpath(layout.run)
    if under(real, [run]) or under(run, [real]):
        raise ConfigError(
            f"{label} {path} overlaps the run dir {layout.run}: the agent must not write it"
        )


@_own
def plan(run: RunConfig, spec: Spec, *, driver: Driver, composition: Composer) -> Plan:
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
    tasks = composition.index(run.role)
    if spec.task:
        if spec.task not in tasks:
            raise ConfigError(
                f"task {spec.task!r} is not one of {run.role}'s tasks ({', '.join(tasks)})"
            )
        run = replace(run, task=spec.task)
    layout = Layout(spec.workdir)
    client = spec.client or run.client
    out = layout.out if driver.session_based else None
    if spec.out:
        out = os.path.abspath(spec.out)
    spec = replace(
        spec,
        workdir=layout.workdir,
        out=out,
        client=client,
        sid=spec.sid if spec.sid is not None else str(uuid.uuid4()),
        team=composition.team,
    )
    resume = spec.sid if spec.resume else None
    here, project = place(
        run, layout, resume_sid=resume, cwd=spec.cwd, trusted=composition.trusted_dirs()
    )
    if (
        driver.session_based
        and not project
        and os.path.realpath(here) != os.path.realpath(layout.agent)
    ):
        print(
            f"delegate: cwd {here} is not in trusted_dirs: its project settings are off",
            file=sys.stderr,
        )
    intent = composition.access(run, layout.agent, cwd=here, project=project, repo_dir=spec.repo)
    for path in intent.dirs:
        _refuse_overlap("write dir", path, layout)
    access = Access(
        dirs=intent.dirs,
        commands=intent.commands + ((intent.gate,) if intent.gate else ()),
        cwd=here,
        project=project,
        reads=intent.reads,
        env=intent.env,
    )
    flags, env = driver.policy(client)
    session = SessionSpec(
        workdir=layout.run,
        client=client,
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
    )
    _check_team_dirs(
        composition, intent.writes, client=client, spec=session, layout=layout, driver=driver
    )
    planned = Plan(spec, session, run)
    return replace(
        planned, session=replace(session, prompt=composition.render(run, planned.params))
    )


def _check_team_dirs(
    composition: Composer,
    write_dirs: tuple[str, ...],
    *,
    client: str,
    spec: SessionSpec,
    layout: Layout,
    driver: Driver,
) -> None:
    """Every dir the run can write counts, those `client` can write itself (writable_dirs) last;
    spec.access.reads, read only, do not.
    """
    if not composition.team.dirs:
        return
    writes = [("the agent dir", layout.agent), ("the cwd", spec.access.cwd)]
    writes += [(f"write dir {p}", p) for p in write_dirs]
    writes += [("a dir the run adds", p) for p in spec.access.dirs]
    writes += [(f"a dir {client} can write", p) for p in driver.writable_dirs(spec)]
    composition.check_team_dirs(writes=writes)
