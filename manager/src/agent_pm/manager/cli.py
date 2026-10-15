"""agent-pm-crew: command-based launches and a manager's persistent roster."""

import argparse
import os
import re
import shlex
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from agent_pm.manager import Crew, Roster, RosterError, Spec, State
from agent_pm.manager.engine.holder import Holder
from agent_pm.manager.engine.types import Lease, StatusError
from agent_pm.utils import STATE, nested_start

SESSION_COMMAND = ("agent-pm-session",)


def _number(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("want a non-negative integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("want a non-negative integer")
    return number


def _after(value: str) -> int | None:
    return None if value == "end" else _number(value)


def _roster(args) -> Roster:
    manager = args.manager or os.environ.get("AGENT_PM_MANAGER")
    if not manager or not re.fullmatch(r"[A-Za-z0-9_-]+", manager):
        raise ValueError("give --manager NAME or set AGENT_PM_MANAGER to a manager name")
    directory = Path(os.path.expanduser(STATE)) / "managers" / manager
    return Roster(str(directory))


def _crew(args) -> Crew:
    holder = os.environ.get("AGENT_PM_HOLDER_ID")
    generation = os.environ.get("AGENT_PM_HOLDER_GENERATION")
    lease = None
    if holder is not None or generation is not None:
        if not holder or generation is None or not re.fullmatch(r"[1-9][0-9]*", generation):
            raise ValueError("AGENT_PM_HOLDER_ID and AGENT_PM_HOLDER_GENERATION must go together")
        identity = uuid.UUID(holder)
        if identity.version != 4 or str(identity) != holder:
            raise ValueError("AGENT_PM_HOLDER_ID must be a canonical UUID4")
        lease = Lease(holder, int(generation))
    return Crew(_roster(args), lease=lease)


def _hold(args) -> int:
    return Holder(_roster(args)).run(args.command, session=args.session, force=args.force)


def _run(args) -> int:
    print(_crew(args).run(Spec(args.name, args.command, args.note)).to_line())
    return 0


def _attach(args) -> int:
    crew = _crew(args)
    lines = crew.attach(after=args.after, gen=args.gen).splitlines()
    trailer = lines.pop()
    cursor, generation = (field.partition("=")[2] for field in trailer.split())
    manager = os.path.basename(crew.roster.directory)
    named = (f"--manager={manager}",) if manager.startswith("-") else ("--manager", manager)
    print("\n".join(lines))
    for name, entry in crew.roster.entries().items():
        if entry.state == State.GONE:
            command = ("agent-pm-crew", "resume", *named, name)
            print(f"resume: {shlex.join(command)}")
    if os.path.isdir(crew.roster.directory):
        print(f"monitor: tail -n +{int(cursor) + 1} -F {shlex.quote(crew.roster.events)}")
        for label, verb, after in (
            ("monitor expired", "attach", "LINE"),
            ("next-event", "next-event", cursor),
        ):
            command = ("agent-pm-crew", verb, *named, "--after", after, "--gen", generation)
            print(f"{label}: {shlex.join(command)}")
    print(trailer)
    return 0


def _status(args) -> int:
    failed = None
    try:
        statuses = _crew(args).status(args.names)
    except StatusError as error:
        statuses, failed = error.statuses, error
    for status in statuses:
        print(status.to_line())
    if failed is not None:
        _report_error(failed)
        return 1
    return 0


def _report_error(error: Exception) -> None:
    print(f"crew: {error}", file=sys.stderr)
    if hasattr(error, "__notes__"):
        for note in error.__notes__:
            print(f"crew: {note}", file=sys.stderr)


def _resume(args) -> int:
    crew = _crew(args)
    failed = 0
    for name in args.names:
        try:
            record = crew.resume(name)
            if record is not None:
                print(record.to_line())
        except (RosterError, RuntimeError, ValueError) as error:
            _report_error(error)
            failed = max(failed, 2 if isinstance(error, ValueError) else 1)
    return failed


def _stop(args) -> int:
    _crew(args).stop(args.name)
    return 0


def _stop_all(args) -> int:
    names = _crew(args).stop_all(dry_run=args.dry_run)
    print("\n".join(names) if names else "nothing to stop")
    return 0


def _forget(args) -> int:
    _crew(args).forget(args.name, force=args.force)
    return 0


def _note(args) -> int:
    _crew(args).note(args.name, args.text)
    return 0


def _release(args) -> int:
    _crew(args).release()
    return 0


def _next_event(args) -> int:
    crew = _crew(args)
    line, event = crew.next_event(after=args.after, gen=args.gen)
    print(f"{line} {event}", flush=True)
    if args.content:
        print(crew.event_content(event, SESSION_COMMAND), end="")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-pm-crew")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manager", help="roster name; default: AGENT_PM_MANAGER")
    subs = parser.add_subparsers(required=True)
    hold = subs.add_parser("hold", parents=[common])
    hold.add_argument("--session", help="holder's display label")
    hold.add_argument("--force", action="store_true", help="take over and fence the old holder")
    hold.set_defaults(func=_hold)
    run = subs.add_parser("run", parents=[common])
    run.add_argument("name")
    run.add_argument("--note")
    run.set_defaults(func=_run)
    attach = subs.add_parser("attach", parents=[common])
    attach.add_argument("--after", type=_number)
    attach.add_argument("--gen", type=_number)
    attach.set_defaults(func=_attach)
    status = subs.add_parser("status", parents=[common])
    status.add_argument("names", nargs="*")
    status.set_defaults(func=_status)
    resume = subs.add_parser("resume", parents=[common])
    resume.add_argument("names", nargs="+")
    resume.set_defaults(func=_resume)
    for name, handler in (("stop", _stop), ("forget", _forget)):
        command = subs.add_parser(name, parents=[common])
        command.add_argument("name")
        if name == "forget":
            command.add_argument(
                "--force",
                action="store_true",
                help="drop bookkeeping without ctl; work may remain alive",
            )
        command.set_defaults(func=handler)
    stop_all = subs.add_parser("stop-all", parents=[common])
    stop_all.add_argument("--dry-run", action="store_true")
    stop_all.set_defaults(func=_stop_all)
    note = subs.add_parser("note", parents=[common])
    note.add_argument("name")
    note.add_argument("text", help="empty text clears the note")
    note.set_defaults(func=_note)
    subs.add_parser("release", parents=[common]).set_defaults(func=_release)
    next_event = subs.add_parser("next-event", parents=[common])
    next_event.add_argument("--after", type=_after, required=True)
    next_event.add_argument("--gen", type=_number, required=True)
    next_event.add_argument(
        "--content",
        action="store_true",
        help="then print its content: a done session's reply, a harness's entry status, "
        "else the screen",
    )
    next_event.set_defaults(func=_next_event)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = ()
    if "--" in arguments:
        delimiter = arguments.index("--")
        command = tuple(arguments[delimiter + 1 :])
        arguments = arguments[:delimiter]
    parser = _parser()
    args = parser.parse_args(arguments)
    if args.func in {_run, _hold}:
        if not command:
            parser.error("run and hold require -- followed by a command")
        args.command = command
    elif command:
        parser.error("only run and hold accept a command after --")
    if args.func is _attach and (args.after is None) != (args.gen is None):
        parser.error("--after and --gen go together")
    if args.func in {_run, _hold, _resume} and (refused := nested_start()):
        print(f"crew: {refused}", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except ValueError as error:
        _report_error(error)
        return 2
    except (OSError, RosterError, RuntimeError) as error:
        _report_error(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
