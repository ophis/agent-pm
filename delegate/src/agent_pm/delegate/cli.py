#!/usr/bin/env python3
"""The Delegate command line: run a role, deliver its instruction with prompt, inspect a plan with
--dry-run, or list the team with --list. Legacy invocation without run remains accepted. Prompt
uses InstructionDriver and leaves session configuration untouched. Runs configure their session;
--out - prints a valid deliverable, otherwise stderr names its file. --detach --events FILE starts
engine.harness in `<role>-<sid8>-drive`, which appends its outcome line when it ends.
Exits 0 on an instruction, valid outcome or detach; 1 without an outcome; 2 on configuration or
usage errors; 3 on client/start failure or usage limit. The harness exits 128 + signum on a signal.
"""

import argparse
import functools
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace

from agent_pm.delegate import Begun, ConfigError, Plan, Spec, composer, configure, plan, run
from agent_pm.delegate.composer import RunParams, Team
from agent_pm.delegate.engine import detach
from agent_pm.delegate.engine.drivers import InstructionDriver
from agent_pm.delegate.engine.runner import say
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError, Mode
from agent_pm.utils import Once, holds, printable

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


def main(argv: list[str], *, application=None) -> int:
    command = argv[0] if argv and argv[0] in ("prompt", "run") else "run"
    if argv and argv[0] in ("prompt", "run"):
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog=f"delegate {command}")
    ap.add_argument("--role")
    ap.add_argument("--task")
    ap.add_argument(
        "--input", metavar="TEXT|-", help="the input text, even when it names a file; -: stdin's"
    )
    ap.add_argument(
        "--out", action=Once, metavar="PATH|-", help="where the deliverable is written; -: stdout"
    )
    ap.add_argument("--workdir", action=Once)
    ap.add_argument("--repo")
    if command == "prompt":
        a = ap.parse_args(argv)
        if a.role is None:
            ap.error("the following arguments are required: --role")
        return _prompt(a, application=application)
    ap.add_argument(
        "--list",
        action="store_true",
        help="list each role, its destination type and tasks, with their sources",
    )
    ap.add_argument("--runtime", action=Once, help="default: the role's runtime in the core config")
    ap.add_argument("--sid")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.HEADLESS.value)
    ap.add_argument("--detach", action="store_true", help="run in a Driver session of its own")
    ap.add_argument("--events", metavar="FILE", help="--detach: the outcome line's file")
    ap.add_argument("--split", choices=("right", "below"))
    ap.add_argument("--split-from", metavar="SESSION")
    ap.set_defaults(harness=None, opener=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.list and (extra := _list_extra(argv)):
        ap.error(f"--list takes no other option, not {extra}")
    if not a.list and a.role is None:
        ap.error("the following arguments are required: --role")
    return (Application() if application is None else application).run_cli(a)[0]


def _prompt(a: argparse.Namespace, *, application=None) -> int:
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


def _list_extra(argv: Sequence[str]) -> str | None:
    return next((arg for arg in argv if not (len(arg) >= 3 and "--list".startswith(arg))), None)


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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
