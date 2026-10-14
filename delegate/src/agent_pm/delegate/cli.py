#!/usr/bin/env python3
"""agent-pm-delegate: run a role on HarnessDelegate, deliver its prompt, or query its runs.
`run --detach` starts the run's harness and prints the run's Record line on stdout;
`--on-event CMD` makes the harness deliver each event to CMD (split once, never a shell). main reads
$AGENT_PM_SID and $AGENT_PM_EVENTS once and removes them from its environment: `run`'s sid is --sid,
else $AGENT_PM_SID (the two must agree); `run --detach`'s events file is --events, else
$AGENT_PM_EVENTS.

Exits 0 on success, 2 on usage or config errors, 1 on other failures.
"""

import argparse
import json
import os
import shlex
import sys
from collections.abc import Sequence
from dataclasses import asdict

from agent_pm.delegate.composer import Composer, RunParams
from agent_pm.delegate.engine.harness_delegate import EVENTS_ENV, SID_ENV, HarnessDelegate
from agent_pm.delegate.engine.runner import report, say
from agent_pm.delegate.engine.types import Begun, ConfigError, Preview, Spec
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError, Mode, NotDisplayable
from agent_pm.utils import Once, answer_keys

LIST_PARAMS = RunParams(input="", out="/agent-pm-list/work/out.md", workdir="/agent-pm-list/work")


def main(
    argv: Sequence[str] | None = None, *, delegate_cls: type[HarnessDelegate] = HarnessDelegate
) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    sid, events = (os.environ.pop(key, "") or None for key in (SID_ENV, EVENTS_ENV))
    parser = argparse.ArgumentParser(prog="agent-pm-delegate")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a role in an agent session")
    _run_options(run)
    for name, many, what in (
        ("status", True, "one Status line per run"),
        ("outcome", False, "the run's outcome as JSON, null without one"),
        ("resume", False, "resume a stopped run; print its Record"),
        ("show", True, "show each run's displayable sessions"),
        ("close", True, "close each run's sessions"),
        ("attach-command", False, "the command attaching to the run"),
    ):
        query = commands.add_parser(name, help=what)
        query.add_argument(
            "keys",
            nargs="*" if many else "?",
            metavar="WORKDIR",
            help="none: one Record per stdin line",
        )
    prompt = commands.add_parser("prompt", help="write a role's prompt as an instruction file")
    _common_options(prompt)
    a = parser.parse_args(argv)
    if a.command == "run":
        return _run(a, run, argv[1:], delegate_cls, sid=sid, events=events)
    if a.command == "prompt":
        return _prompt(a, prompt, delegate_cls)
    return _query(a, delegate_cls)


def _common_options(ap: argparse.ArgumentParser) -> None:
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


def _run_options(ap: argparse.ArgumentParser) -> None:
    _common_options(ap)
    ap.add_argument(
        "--list",
        action="store_true",
        help="list each role, its destination type and tasks, with their sources",
    )
    ap.add_argument("--client", action=Once, help="default: the role's client in the core config")
    ap.add_argument("--sid", help="default $AGENT_PM_SID; the two must agree")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.HEADLESS.value)
    ap.add_argument("--detach", action="store_true", help="run in a harness session of its own")
    ap.add_argument(
        "--events", metavar="FILE", help="--detach: the run's events file; default $AGENT_PM_EVENTS"
    )
    ap.add_argument(
        "--on-event",
        action=Once,
        type=_command,
        metavar="CMD",
        help="--detach: the command each event's JSON line goes to on stdin",
    )
    ap.add_argument(
        "--display",
        metavar="D",
        help="where its sessions show, completed on its agent's Host: agent-pm-session --help",
    )
    ap.add_argument("--dry-run", action="store_true")


def _run(
    a: argparse.Namespace,
    ap: argparse.ArgumentParser,
    argv: Sequence[str],
    delegate_cls: type[HarnessDelegate],
    *,
    sid: str | None,
    events: str | None,
) -> int:
    if a.list:
        if extra := next(
            (arg for arg in argv if not "--list".startswith(arg) or len(arg) < 3), None
        ):
            ap.error(f"--list takes no other option, not {extra}")
        try:
            print(listing(Composer()), end="")
        except ConfigError as e:
            say(f"delegate: {e}")
            return 2
        return 0
    if a.role is None:
        ap.error("the following arguments are required: --role")
    if a.detach and a.events is None:
        a.events = events
    try:
        if sid is not None and a.sid not in (None, sid):
            raise ConfigError(f"${SID_ENV} {sid} differs from --sid {a.sid}")
        a.sid = sid if a.sid is None else a.sid
        _check(a)
    except ConfigError as e:
        say(f"delegate: {e}")
        return 2
    spec = spec_of(a, sys.stdin.read() if a.input == "-" else a.input)
    placement = {"display": a.display}
    if a.on_event is not None:
        placement["callback"] = a.on_event
    try:
        if a.dry_run:
            shown = _shown(delegate_cls.preview(a.role, spec, **placement), a)
            print(json.dumps(shown, ensure_ascii=False, indent=1))
            return 0
        if a.detach:
            return _detach(delegate_cls, a.role, spec, placement)
        result = delegate_cls(a.role, spec, display=a.display).run(_begun)
    except ConfigError as e:
        say(f"delegate: {e}")
        return 2
    return report(result, a.out)[0]


def _detach(delegate_cls: type[HarnessDelegate], role: str, spec: Spec, placement: dict) -> int:
    """Prints the run's Record line once its listed sessions are live."""
    try:
        record = delegate_cls(role, spec, **placement).detach()
    except (DriverError, OSError) as e:
        say(f"delegate: {e}")
        return 1
    say(f"delegate: session {record.sid}")
    print(record.to_line(), flush=True)
    return 0


def spec_of(a, text: str) -> Spec:
    """The Spec `run`'s options name, its input `text`."""
    return Spec(
        text,
        a.workdir,
        task=a.task,
        mode=a.mode,
        client=a.client,
        sid=a.sid,
        resume=a.resume,
        events=a.events,
        out=None if a.out == "-" else a.out,
        repo=a.repo,
    )


def _check(a: argparse.Namespace) -> None:
    if None in (a.input, a.out, a.workdir):
        raise ConfigError("--input, --out and --workdir are required")
    if a.resume and a.sid is None:
        raise ConfigError("--resume needs --sid")
    if a.events is not None and not a.detach:
        raise ConfigError("--events needs --detach")
    if a.on_event is not None and not a.detach:
        raise ConfigError("--on-event needs --detach")
    if a.detach and a.events is None:
        raise ConfigError("--detach needs --events")
    if a.detach and a.out == "-":
        raise ConfigError("--detach needs --out PATH")
    if a.detach and a.mode == Mode.IN_PROCESS:
        raise ConfigError("--detach needs --mode headless or tui")


def _command(text: str) -> tuple[str, ...]:
    try:
        argv = tuple(shlex.split(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    if not argv:
        raise argparse.ArgumentTypeError("empty command")
    return argv


def _begun(event) -> None:
    if isinstance(event, Begun):
        say(f"delegate: session {event.sid}")


def _shown(preview: Preview, a: argparse.Namespace) -> dict:
    access, s = preview.plan.session.access, preview.plan.spec
    shown = {
        "client": s.client,
        "mode": s.mode,
        "sid": s.sid,
        "resume": s.resume,
        "cwd": access.cwd,
        "project": access.project,
        "dirs": access.dirs,
        "reads": access.reads,
        "commands": access.commands,
        "tier": preview.plan.session.tier,
        "effort": preview.plan.session.effort,
        "prompt": preview.plan.prompt,
        "sessions": {"agent": preview.agent, "harness": preview.harness if a.detach else None},
        "hosts": {"agent": preview.host, "harness": preview.harness_host},
        "callback": preview.callback,
    }
    if a.detach:
        shown["events"] = os.path.abspath(a.events)
    return shown


def _prompt(
    a: argparse.Namespace, ap: argparse.ArgumentParser, delegate_cls: type[HarnessDelegate]
) -> int:
    if a.role is None:
        ap.error("the following arguments are required: --role")
    try:
        if None in (a.input, a.out, a.workdir):
            raise ConfigError("--input, --out and --workdir are required")
        if a.out == "-":
            raise ConfigError("prompt needs --out PATH")
        spec = Spec(
            sys.stdin.read() if a.input == "-" else a.input,
            a.workdir,
            task=a.task,
            out=a.out,
            repo=a.repo,
        )
        result = delegate_cls.instruct(a.role, spec)
    except (ConfigError, OSError) as error:
        say(f"delegate: {error}")
        return 2
    say(f"delegate: instruction delivered: {result.instruction}")
    return 0


def _query(a: argparse.Namespace, delegate_cls: type[HarnessDelegate]) -> int:
    single = a.command in ("outcome", "resume", "attach-command")
    keys = ([] if a.keys is None else [a.keys]) if single else a.keys

    def viewed(key):
        view = delegate_cls.attach(key)
        if single:
            return "", view
        status = view.status()
        return status.name, (view, status)

    reply = {
        "status": lambda pair: pair[1].to_line(),
        "show": lambda pair: pair[0].show(),
        "close": lambda pair: pair[0].close(),
        "outcome": _outcome,
        "resume": lambda view: view.resume().to_line(),
        "attach-command": _attach_command,
    }[a.command]
    return answer_keys(
        keys,
        sys.stdin,
        viewed,
        reply,
        single=single,
        usage=(ConfigError, SessionConfigError),
        failures=(DriverError, OSError),
        prog="delegate",
        out=sys.stdout,
        err=sys.stderr,
    )


def _outcome(view) -> str:
    outcome = view.outcome()
    return json.dumps(None if outcome is None else asdict(outcome), separators=(",", ":"))


def _attach_command(view) -> str:
    command = view.attach_command()
    if command is None:
        raise NotDisplayable("no displayable session")
    return command


def listing(composer: Composer) -> str:
    """Each role and its tasks with their sources; checks every file and prompt, writing nothing."""
    composer.check_team_dirs(writes=[])
    composer.check_all()
    lines = []
    for role, tasks in composer.catalog().items():
        config = composer.resolve(role)
        composer.render(config, LIST_PARAMS)
        source = composer.team.source(composer.path_for("roles", role))
        lines.append(f"{role}  {config.output['type']}  {source}\n")
        lines += [
            f"  {t}: {what}  [{composer.team.source(composer.path_for('tasks', t))}]\n"
            for t, what in tasks.items()
        ]
    return "".join(lines)


if __name__ == "__main__":
    sys.exit(main())
