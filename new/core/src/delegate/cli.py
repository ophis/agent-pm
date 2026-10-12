#!/usr/bin/env python3
"""The Delegate's command line: one role's run in a Driver Mode (--mode: headless, the default, tui or in_process), its
plan as JSON (--dry-run, writing nothing) or the team (--list: the built-in one and the core config's team_dirs).
--task unset: the agent run picks one from its index. --out -: a valid outcome's deliverable goes to stdout, control
characters dropped but newlines and tabs; --out PATH: stderr names it once it holds the deliverable. A run's stdout is
otherwise empty. Exits 0 on a valid outcome, even when the client exited non-zero; 1 on none, the client exiting 0 or
its tui session left open; 2 on a config or usage error (nothing started, stdout empty); 3 when the client exited
non-zero (an in_process run that raised: 1) or failed to start without a valid outcome, or on a usage limit."""

import argparse
import json
import os
import sys
from collections.abc import Sequence

if __name__ == "__main__":  # as a script: src/ first, in place of this dir
    _HERE = os.path.dirname(os.path.realpath(__file__))
    sys.path[:] = [
        os.path.dirname(_HERE),
        *(p for p in sys.path if os.path.realpath(p or os.curdir) != _HERE),
    ]

import composer  # noqa: E402
from composer import RunParams, Team  # noqa: E402
from driver import api  # noqa: E402
from utils.args import Once  # noqa: E402

from delegate import Begun, ConfigError, Plan, plan, run  # noqa: E402
from delegate.runner import printable  # noqa: E402

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
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.list and (extra := _list_extra(argv)):
        ap.error(f"--list takes no other option, not {extra}")
    if not a.list and a.role is None:
        ap.error("the following arguments are required: --role")
    try:
        if not a.list and None in (a.input, a.out, a.workdir):
            raise ConfigError("--input, --out and --workdir are required")
        if a.resume and a.sid is None:
            raise ConfigError("--resume needs --sid")
        team = composer.team(composer.TEAM, composer.team_dirs())
        if a.list:
            print(listing(team), end="")
            return 0
        config = composer.resolve(a.role, a.task, team=team)
        given = (config, a.task, sys.stdin.read() if a.input == "-" else a.input, a.workdir)
        kw = {
            "runtime": a.runtime or config.runtime,
            "mode": a.mode,
            "sid": a.sid,
            "resume": a.sid if a.resume else None,
            "out": None if a.out == "-" else a.out,
            "repo": a.repo,
            "team": team,
        }
        if a.dry_run:
            print(json.dumps(_shown(plan(*given, **kw)), ensure_ascii=False, indent=1))
            return 0
        result = run(*given, on_event=_begun, **kw)
    except (ConfigError, composer.ConfigError) as e:
        _say(f"delegate: {e}")
        return 2
    if result.outcome is None:
        _say(f"delegate: {result.error}")
        return 3 if result.returncode or result.limit else 1
    if deliverable := result.outcome.deliverable:
        if a.out == "-":
            print("\n".join(map(printable, deliverable.split("\n"))), end="", flush=True)
        elif api.records.holds(out := os.path.abspath(a.out), deliverable):
            _say(f"delegate: deliverable {out}")
    _say(f"delegate: status {result.outcome.status}")
    return 0


def listing(team: Team) -> str:
    """Each role and its tasks with their sources; checks every file and prompt, writing nothing."""
    composer.check_team_dirs(team.dirs, writes=[])
    team.check_all({})
    lines = []
    for role, tasks in composer.catalog(team).items():
        config = composer.resolve(role, team=team)
        composer.render(config, LIST_PARAMS, team=team)
        lines.append(
            f"{role}  {config.output['type']}  {team.source(team.lookup('roles', role))}\n"
        )
        lines += [
            f"  {t}: {what}  [{team.source(team.lookup('tasks', t))}]\n"
            for t, what in tasks.items()
        ]
    return "".join(lines)


def _list_extra(argv: Sequence[str]) -> str | None:
    return next((arg for arg in argv if not (len(arg) >= 3 and "--list".startswith(arg))), None)


def _shown(p: Plan) -> dict:
    access = p.spec.access
    return {
        "runtime": p.runtime,
        "mode": p.mode,
        "sid": p.sid,
        "resume": p.resume,
        "cwd": access.cwd,
        "project": access.project,
        "dirs": access.dirs,
        "reads": access.reads,
        "commands": access.commands,
        "tier": p.spec.tier,
        "effort": p.spec.effort,
        "prompt": p.prompt,
    }


def _begun(event) -> None:
    if isinstance(event, Begun):
        _say(f"delegate: session {event.sid}")


def _say(text: str) -> None:
    print(printable(text), file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
