#!/usr/bin/env python3
"""agent-pm-delegate: run a role on HarnessDelegate, deliver its prompt, or query its runs.

Exits 0 on success, 2 on usage or config errors, 1 on other failures. `run --detach` takes the old
path (engine.application) until 356-7e.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict

from agent_pm.delegate.composer import Composer, RunParams
from agent_pm.delegate.engine.application import Application
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.delegate.engine.runner import report, say
from agent_pm.delegate.engine.types import Begun, ConfigError, Preview, Spec
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError, Mode, NotDisplayable
from agent_pm.utils import Once, answer_keys

LIST_PARAMS = RunParams(input="", out="/agent-pm-list/work/out.md", workdir="/agent-pm-list/work")
SPLIT = "--split and --split-from need --detach --mode tui"


def main(
    argv: Sequence[str] | None = None,
    *,
    delegate_cls: type[HarnessDelegate] = HarnessDelegate,
    application: Application | None = None,
) -> int:
    """`application` serves only `run --detach`."""
    argv = sys.argv[1:] if argv is None else list(argv)
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
        return _run(a, run, argv[1:], delegate_cls, application)
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
    ap.add_argument("--sid")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.HEADLESS.value)
    ap.add_argument("--detach", action="store_true", help="run in a Driver session of its own")
    ap.add_argument("--events", metavar="FILE", help="--detach: the outcome line's file")
    ap.add_argument("--split", choices=("right", "below"))
    ap.add_argument("--split-from", metavar="SESSION")
    ap.add_argument("--dry-run", action="store_true")


def _run(
    a: argparse.Namespace,
    ap: argparse.ArgumentParser,
    argv: Sequence[str],
    delegate_cls: type[HarnessDelegate],
    application: Application | None,
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
    try:
        _check(a)
    except ConfigError as e:
        say(f"delegate: {e}")
        return 2
    spec = spec_of(a, sys.stdin.read() if a.input == "-" else a.input)
    if a.detach and not a.dry_run:
        a.harness = a.opener = None
        return (application or Application()).run_cli(a, spec)[0]
    try:
        if a.dry_run:
            shown = _shown(delegate_cls.preview(a.role, spec), a)
            print(json.dumps(shown, ensure_ascii=False, indent=1))
            return 0
        result = delegate_cls(a.role, spec).run(_begun)
    except ConfigError as e:
        say(f"delegate: {e}")
        return 2
    return report(result, a.out)[0]


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
        events=a.events if a.mode == "tui" else None,
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
    if a.detach and a.events is None:
        raise ConfigError("--detach needs --events")
    if (a.split is not None or a.split_from is not None) and not (a.detach and a.mode == "tui"):
        raise ConfigError(SPLIT)
    if a.detach and a.out == "-":
        raise ConfigError("--detach needs --out PATH")


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
