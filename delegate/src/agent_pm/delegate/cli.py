#!/usr/bin/env python3
"""The Delegate's command line: one role's run in a Driver Mode (--mode: headless, the default, tui or in_process), its
plan as JSON (--dry-run, writing nothing) or the team (--list: the built-in one and the core config's team_dirs). All
but --list first configure the session from the session config.
--task unset: the agent run picks one from its index. --out -: a valid outcome's deliverable goes to stdout, control
characters dropped but newlines and tabs; --out PATH: stderr names it once it holds the deliverable. A run's stdout is
otherwise empty. --detach --events FILE: the run goes on in Driver session `<role>-<sid8>-drive` as this CLI's driver
form (--driver), which appends `HH:MM:SS <that session> outcome <status>` to FILE as it ends, `error` without a valid
outcome. Exits 0 on a valid outcome, even when the client exited non-zero, or once detached; 1 on none, the client
exiting 0 or its tui session left open; 2 on a config or usage error (nothing started, stdout empty); 3 when the client
exited non-zero (an in_process run that raised: 1) or failed to start without a valid outcome, on a usage limit, or
when the Driver session did not start; the driver form 128 + signum on a signal."""

import argparse
import contextlib
import functools
import json
import os
import signal
import sys
from collections.abc import Iterator, Sequence
from dataclasses import replace

from agent_pm.delegate import Begun, ConfigError, Plan, Spec, composer, plan, run
from agent_pm.delegate.composer import RunParams, Team
from agent_pm.delegate.engine import detach
from agent_pm.delegate.engine.config import harness_host
from agent_pm.delegate.engine.runner import printable
from agent_pm.session import Session, api
from agent_pm.utils.args import Once
from agent_pm.utils.config_file import read_project

LIST_PARAMS = RunParams(input="", out="/agent-pm-list/work/out.md", workdir="/agent-pm-list/work")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="delegate")
    ap.add_argument(
        "--list",
        action="store_true",
        help="list each role, its destination type and tasks, with their sources",
    )
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
    ap.add_argument("--runtime", action=Once, help="default: the role's runtime in the core config")
    ap.add_argument("--sid")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mode", choices=api.MODES, default="headless")
    ap.add_argument("--detach", action="store_true", help="run in a Driver session of its own")
    ap.add_argument("--events", metavar="FILE", help="--detach: the outcome line's file")
    ap.add_argument("--split", choices=("right", "below"))
    ap.add_argument("--split-from", metavar="SESSION")
    ap.add_argument("--driver", help=argparse.SUPPRESS)
    ap.add_argument("--opener", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.list and (extra := _list_extra(argv)):
        ap.error(f"--list takes no other option, not {extra}")
    if not a.list and a.role is None:
        ap.error("the following arguments are required: --role")
    if a.driver is None:
        return _main(a)[0]
    if None in (a.events, a.sid):
        ap.error("--driver needs --events and --sid")
    status = None
    try:
        for s in api.SIGNALS:
            signal.signal(s, _stop)
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, api.SIGNALS)  # api.detach blocked them
            code, status = _main(a)
            return code
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, api.SIGNALS)
    finally:
        detach.outcome(a.events, a.driver, status)


def _main(a: argparse.Namespace) -> tuple[int, str | None]:
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
        _configure()
        text = sys.stdin.read() if a.input == "-" and a.driver is None else a.input
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
            return _detach(a, plan(config, spec)), None
        if a.dry_run:
            print(json.dumps(_shown(plan(config, spec)), ensure_ascii=False, indent=1))
            return 0, None
        tui = detach.names(config.role, a.sid, a.mode)[0] if a.driver else None
        spec = replace(spec, name=tui)
        result = run(config, spec, on_event=functools.partial(_begun, a, tui))
    except ConfigError as e:
        _say(f"delegate: {e}")
        return 2, None
    if result.outcome is None:
        _say(f"delegate: {result.error}")
        return (3 if result.returncode or result.limit else 1), None
    if deliverable := result.outcome.deliverable:
        if a.out == "-":
            print("\n".join(map(printable, deliverable.split("\n"))), end="", flush=True)
        elif api.records.holds(out := os.path.abspath(a.out), deliverable):
            _say(f"delegate: deliverable {out}")
    _say(f"delegate: status {result.outcome.status}")
    return 0, result.outcome.status


def _configure() -> None:
    with _config("session"):
        table = read_project("session")
        hosts = Session.parse_hosts(table.get("hosts")).tables
    with _config("delegate"):
        harness_host(read_project("delegate"), hosts)
    with _config("session"):
        Session.configure_from(table)


@contextlib.contextmanager
def _config(name: str) -> Iterator[None]:
    try:
        yield
    except (OSError, ValueError, api.ConfigError) as e:
        raise ConfigError(f"{name} config: {e}") from None


def _check_detach(a: argparse.Namespace) -> None:
    detached = a.detach or a.driver is not None
    if a.events is not None and not detached:
        raise ConfigError("--events needs --detach")
    if a.detach and a.events is None:
        raise ConfigError("--detach needs --events")
    if (a.split is not None or a.split_from is not None) and not (detached and a.mode == "tui"):
        raise ConfigError("--split and --split-from need --detach --mode tui")
    if a.detach and a.out == "-":
        raise ConfigError("--detach needs --out PATH")


def _detach(a: argparse.Namespace, p: Plan) -> int:
    tui, driver = detach.names(p.run.role, p.spec.sid, p.spec.mode)
    if a.dry_run:
        shown = {**_shown(p), "driver": driver, "tui": tui, "events": os.path.abspath(a.events)}
        print(json.dumps(shown, ensure_ascii=False, indent=1))
        return 0
    try:
        d = detach.detach(p, events=a.events, split=a.split, split_from=a.split_from)
    except api.DriverError as e:
        _say(f"delegate: {e}")
        return 3
    _say(f"delegate: session {p.spec.sid}")
    _say(f"delegate: driver session {d.driver}: {api.attach_command(d.driver)}")
    if d.tui is not None:
        _say(f"delegate: tui session {d.tui}: {api.attach_command(d.tui)}")
    return 0


def _stop(signum, frame):
    signal.pthread_sigmask(signal.SIG_BLOCK, api.SIGNALS)  # one exit: one outcome line
    raise SystemExit(128 + signum)


def listing(team: Team) -> str:
    """Each role and its tasks with their sources; checks every file and prompt, writing nothing."""
    composer.check_team_dirs(team.dirs, writes=[])
    composer.check_all(team, {})
    lines = []
    for role, tasks in composer.catalog(team).items():
        config = composer.resolve(role, team=team)
        composer.render(config, LIST_PARAMS, team=team)
        lines.append(
            f"{role}  {config.output['type']}  {team.source(composer.lookup(team, 'roles', role))}\n"
        )
        lines += [
            f"  {t}: {what}  [{team.source(composer.lookup(team, 'tasks', t))}]\n"
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


def _begun(a: argparse.Namespace, tui: str | None, event) -> None:
    if not isinstance(event, Begun):
        return
    _say(f"delegate: session {event.sid}")
    if tui is not None and a.opener is not None:
        try:
            if command := api.show(tui, a.split, a.split_from, a.opener):
                _say(command)
        except (api.ConfigError, api.DriverError) as e:
            _say(f"delegate: show {tui}: {e}")


def _say(text: str) -> None:
    print(printable(text), file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
