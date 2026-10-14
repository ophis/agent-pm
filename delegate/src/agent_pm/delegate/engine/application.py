"""Delegate application behavior shared by command-line entry and detached harness."""

import argparse
import functools
import json
import os
import sys
from dataclasses import replace

from agent_pm.delegate import composer
from agent_pm.delegate.composer import RunParams, Team
from agent_pm.delegate.engine import detach
from agent_pm.delegate.engine.drivers import InstructionDriver
from agent_pm.delegate.engine.plan import configure, plan
from agent_pm.delegate.engine.runner import run, say
from agent_pm.delegate.engine.types import Begun, ConfigError, Plan, Spec
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError
from agent_pm.utils import holds, printable

LIST_PARAMS = RunParams(input="", out="/agent-pm-list/work/out.md", workdir="/agent-pm-list/work")


class Application:
    """The configured session owner for command-line planning and execution."""

    def __init__(self, driver=None):
        self._driver = driver

    @property
    def driver(self):
        if self._driver is None:
            self._driver = configure()
        return self._driver

    def plan(self, config, spec):
        return plan(config, spec, driver=self.driver)

    def run(self, config, spec, *, on_event):
        return run(config, spec, driver=self.driver, on_event=on_event)

    def run_cli(self, a: argparse.Namespace) -> tuple[int, str | None]:
        """(exit code, the valid outcome's status)."""
        try:
            if not a.list and None in (a.input, a.out, a.workdir):
                raise ConfigError("--input, --out and --workdir are required")
            if a.resume and a.sid is None:
                raise ConfigError("--resume needs --sid")
            _check_detach(a)
            team = composer.team(composer.TEAM, composer.team_dirs())
            if a.list:
                print(listing(team), end="")
                return 0, None
            config = composer.resolve(a.role, a.task, team=team)
            driver = self.driver
            text = sys.stdin.read() if a.input == "-" and a.harness is None else a.input
            spec = Spec(
                text,
                a.workdir,
                task=a.task,
                mode=a.mode,
                runtime=a.runtime,
                sid=a.sid,
                resume=a.resume,
                events=a.events if a.mode == "tui" else None,
                out=None if a.out == "-" else a.out,
                repo=a.repo,
                team=team,
            )
            if a.detach:
                return _detach(a, self.plan(config, spec), driver), None
            if a.dry_run:
                print(json.dumps(_shown(self.plan(config, spec)), ensure_ascii=False, indent=1))
                return 0, None
            tui = detach.names(config.role, a.sid, a.mode)[0] if a.harness else None
            spec = replace(spec, name=tui)
            result = self.run(config, spec, on_event=functools.partial(_begun, a, tui, driver))
        except ConfigError as e:
            say(f"delegate: {e}")
            return 2, None
        if result.outcome is None:
            say(f"delegate: {result.error}")
            return (3 if result.returncode or result.limit else 1), None
        text = result.outcome.deliverable
        if text:
            if a.out == "-":
                print("\n".join(map(printable, text.split("\n"))), end="", flush=True)
            elif holds(path := os.path.abspath(a.out), text):
                say(f"delegate: deliverable {path}")
        say(f"delegate: status {result.outcome.status}")
        return 0, result.outcome.status


def prompt(a: argparse.Namespace, *, application=None) -> int:
    try:
        if None in (a.input, a.out, a.workdir):
            raise ConfigError("--input, --out and --workdir are required")
        if a.out == "-":
            raise ConfigError("prompt needs --out PATH")
        team = composer.team(composer.TEAM, composer.team_dirs())
        config = composer.resolve(a.role, a.task, team=team)
        application = Application(InstructionDriver(a.out)) if application is None else application
        spec = Spec(
            sys.stdin.read() if a.input == "-" else a.input,
            a.workdir,
            task=a.task,
            out=a.out,
            repo=a.repo,
            team=team,
        )
        result = application.run(config, spec, on_event=None)
    except (ConfigError, OSError) as error:
        say(f"delegate: {error}")
        return 2
    say(f"delegate: instruction delivered: {result.instruction}")
    return 0


def _check_detach(a: argparse.Namespace) -> None:
    detached = a.detach or a.harness is not None
    if a.events is not None and not detached:
        raise ConfigError("--events needs --detach")
    if a.detach and a.events is None:
        raise ConfigError("--detach needs --events")
    if (a.split is not None or a.split_from is not None) and not (detached and a.mode == "tui"):
        raise ConfigError("--split and --split-from need --detach --mode tui")
    if a.detach and a.out == "-":
        raise ConfigError("--detach needs --out PATH")


def _detach(a: argparse.Namespace, p: Plan, driver) -> int:
    tui, harness = detach.names(p.run.role, p.sid, p.spec.mode)
    if a.dry_run:
        shown = {**_shown(p), "driver": harness, "tui": tui, "events": os.path.abspath(a.events)}
        print(json.dumps(shown, ensure_ascii=False, indent=1))
        return 0
    try:
        d = detach.detach(p, driver=driver, events=a.events, split=a.split, split_from=a.split_from)
    except DriverError as e:
        say(f"delegate: {e}")
        return 3
    say(f"delegate: session {p.spec.sid}")
    say(f"delegate: driver session {d.driver}: {driver.attach_command(d.driver)}")
    if d.tui is not None:
        say(f"delegate: tui session {d.tui}: {driver.attach_command(d.tui)}")
    return 0


def listing(team: Team) -> str:
    """Each role and its tasks with their sources; checks every file and prompt, writing nothing."""
    composer.check_team_dirs(team.dirs, writes=[])
    composer.check_all(team, {})
    lines = []
    for role, tasks in composer.catalog(team).items():
        config = composer.resolve(role, team=team)
        composer.render(config, LIST_PARAMS, team=team)
        lines.append(
            f"{role}  {config.output['type']}  {team.source(composer.path_for(team, 'roles', role))}\n"
        )
        lines += [
            f"  {t}: {what}  [{team.source(composer.path_for(team, 'tasks', t))}]\n"
            for t, what in tasks.items()
        ]
    return "".join(lines)


def _shown(p: Plan) -> dict:
    access, s = p.session.access, p.spec
    return {
        "runtime": s.runtime,
        "mode": s.mode,
        "sid": s.sid,
        "resume": s.resume,
        "cwd": access.cwd,
        "project": access.project,
        "dirs": access.dirs,
        "reads": access.reads,
        "commands": access.commands,
        "tier": p.session.tier,
        "effort": p.session.effort,
        "prompt": p.prompt,
    }


def _begun(a: argparse.Namespace, tui: str | None, driver, event) -> None:
    if not isinstance(event, Begun):
        return
    say(f"delegate: session {event.sid}")
    if tui is not None and a.opener is not None:
        try:
            if command := driver.show(tui, a.split, a.split_from, a.opener):
                say(command)
        except (SessionConfigError, DriverError) as e:
            say(f"delegate: show {tui}: {e}")
