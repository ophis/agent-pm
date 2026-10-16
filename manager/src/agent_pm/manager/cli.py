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


def _launch(args) -> int:
    record = _crew(args).launch(
        args.kind, args.key, tuple(args.arguments), note=args.note, name=args.entry_name
    )
    print(record.to_line())
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
        for label, verb, after, *extra in (
            ("monitor", "next-event", cursor, "--content", "--follow"),
            ("monitor expired", "attach", "LINE"),
            ("next-event", "next-event", cursor, "--content"),
        ):
            command = ("agent-pm-crew", verb, *named, "--after", after, "--gen", generation, *extra)
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


def _report(args) -> int:
    print(_crew(args).report(), end="")
    return 0


def _resume(args) -> int:
    crew = _crew(args)
    failed = 0
    for name in args.names:
        try:
            record = crew.resume(name, input=args.input)
            if record is not None:
                print(record.to_line())
        except (RosterError, RuntimeError, ValueError) as error:
            _report_error(error)
            failed = max(failed, 2 if isinstance(error, ValueError) else 1)
    return failed


def _view(args) -> int:
    failures = _crew(args).view(args.verb, args.names)
    for name, failure in failures.items():
        print(f"crew: {name}: {failure}", file=sys.stderr)
    return 1 if failures else 0


def _close(args) -> int:
    _crew(args).close(args.name)
    return 0


def _close_all(args) -> int:
    names = _crew(args).close_all(dry_run=args.dry_run)
    print("\n".join(names) if names else "nothing to close")
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
    after = args.after
    while True:
        after, event = crew.next_event(after=after, gen=args.gen)
        block = f"{after} {event}\n"
        if args.content:
            block += crew.event_content(event)
        # One write per event: Monitor groups lines written within 200 ms into one notification.
        print(block, end="", flush=True)
        if not args.follow:
            return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-pm-crew",
        description="Launch workers and role/issue runs; manage their persistent roster.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Manager: roster selected by --manager, else AGENT_PM_MANAGER; required.
Names use [A-Za-z0-9_-]+. An Entry is a name tracked in that roster.
Crew consumes names, not Records on stdin. A Record is the serialized control
and identity line returned by a launch: agent-pm-session --help.

Prerequisites and effects:
  Install agent-pm commands on PATH; configure the owning client/run layer.
  hold gives its child AGENT_PM_MANAGER and the paired AGENT_PM_HOLDER_ID /
  AGENT_PM_HOLDER_GENERATION. Keep that pair together. An active holder fences
  other identities; run mutating commands from its commander environment.
  AGENT_PM_AGENT_SESSION blocks hold/run/worker/issue/role/resume; use a
  user-started commander. Help still works. State: ~/.agent-pm/managers/NAME/.
  Exit 0: success; 2: usage/invalid value; 1: operational/lease failure.
  hold instead propagates its child's status, signals as 128 + signal number.
  Scalar options use the last value; boolean flags may repeat.

Examples:
  agent-pm-crew worker --help
  agent-pm-crew hold --help

Next help:
  agent-pm-session --help          workers, clients, hosts and placement
  agent-pm-delegate run --help     roles and tasks
  agent-pm-linear router --help    issue runs
""",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--manager", help="roster name; default: AGENT_PM_MANAGER; required from one source"
    )
    subs = parser.add_subparsers(required=True)

    def page(name, purpose, details, **options):
        return subs.add_parser(
            name,
            parents=[common],
            help=purpose,
            description=purpose + ".",
            epilog=details + "\nRoster, environment and exits: agent-pm-crew --help\n",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            **options,
        )

    hold = page(
        "hold",
        "Keep a manager's holder lease while COMMAND runs in the foreground",
        """Prerequisites and effects:
  COMMAND [ARG ...] is required after --, executed without a shell.
  The child inherits manager/lease identity; the lease ends when it exits.
  --session labels the holder; it does not select a terminal to attach.
  By default a holder conflict is refused. --force deliberately takes over.

Examples:
  With Codex on PATH, from a user-started terminal:
  agent-pm-crew hold --manager demo --session commander -- codex
  Inside that commander's environment, reconcile its roster:
  agent-pm-crew attach --manager demo

Recovery and next help:
  Conflict: use the existing commander, or --force for deliberate takeover.
  Reconciliation does not acquire a holder: agent-pm-crew attach --help
""",
        usage="%(prog)s [--manager MANAGER] [--session LABEL] [--force] -- COMMAND [ARG ...]",
    )
    hold.add_argument("--session", metavar="LABEL", help="optional holder display label")
    hold.add_argument("--force", action="store_true", help="take over and fence the old holder")
    hold.set_defaults(func=_hold)
    run = page(
        "run",
        "Run a Record-producing starter and track it as NAME",
        """Prerequisites and effects:
  COMMAND [ARG ...] after -- is required; no shell expansion is performed.
  The starter must finish with a Record on stdout and honor AGENT_PM_SID.
  Crew supplies a new identity and roster events file, records launch intent
  before executing, then prints the Record. A failed launch may remain lost.
  Put Crew options before NAME, then --, then the starter and its options.
  Outer help: agent-pm-crew run --help. Help after -- runs the starter path.

Examples:
  With Codex configured and no helper entry or session:
  agent-pm-crew run --manager demo helper -- \\
    agent-pm-session start helper --client codex --display none

Recovery and next help:
  Inspect/reconcile failed launches: agent-pm-crew attach --help
  Starter options: agent-pm-session start --help
""",
        usage="%(prog)s [--manager MANAGER] [--note TEXT] NAME -- COMMAND [ARG ...]",
    )
    run.add_argument("name", metavar="NAME", help="new roster entry name")
    run.add_argument("--note", metavar="TEXT", help="optional roster note; default: none")
    run.set_defaults(func=_run)
    for kind, key, purpose, owner, details in (
        (
            "worker",
            "NAME",
            "Start a named worker and track its Record",
            "agent-pm-session start",
            """NAME is both the worker's session name and roster entry name.
Session options go after NAME; --client CLIENT is required there.
Session passes CLIENT_FLAGS after its -- verbatim to the client.

Examples:
  With Codex configured and no helper session/entry:
  agent-pm-crew worker --manager demo helper --client codex --display none
  Starts a hidden worker, records it in demo and prints its Record.
  Form: worker [CREW_OPTIONS] NAME [SESSION_OPTIONS] [-- CLIENT_FLAGS]
  Client flag help: use the configured client executable with --help.
  Placement: agent-pm-session --help
""",
        ),
        (
            "issue",
            "ISSUE",
            "Start an attended issue run and track its Record",
            "agent-pm-linear router",
            """ISSUE is a configured board issue identifier and the roster entry name.
Crew supplies router --issue ISSUE --tui, a session identity and events file.
Requires board credentials, a configured role/client and an eligible issue;
execution can claim/change the board and launch agent/harness sessions.

Examples:
  With configured board access and eligible test issue TASK-123:
  agent-pm-crew issue --manager demo TASK-123 --display none
  Starts the run hidden and prints its Record.
  Form: issue [CREW_OPTIONS] ISSUE [ROUTER_OPTIONS]
  There is no client-flags delimiter at this boundary.
  Placement: agent-pm-session --help
""",
        ),
        (
            "role",
            "ROLE",
            "Start a detached role run and track its Record",
            "agent-pm-delegate run",
            """ROLE selects a persona; downstream --task optionally selects its work type.
Crew supplies --role ROLE --detach --name NAME, identity and events file.
Requires configured role/client and downstream --input. Prints a Record.

Examples:
  With the built-in pm role and its client configured:
  agent-pm-crew role --manager demo --name planner pm --input 'Plan a feature'
  Form: role [CREW_OPTIONS] ROLE [DELEGATE_OPTIONS]
  There is no client-flags delimiter at this boundary.
  Discover roles/tasks: agent-pm-delegate run --list
  Placement: agent-pm-session --help
""",
        ),
    ):
        launch = page(
            kind,
            purpose,
            f"Crew options go before {key}; downstream options go after it.\n"
            f"Get outer help before supplying {key}: agent-pm-crew {kind} --help\n"
            "Forwarded --help belongs to the destination and may enter Crew's launch\n"
            "path, which records intent. Use the owning help directly.\n\n"
            + details
            + f"\nDownstream options: {owner} --help\n",
        )
        launch.add_argument("--note", metavar="TEXT", help="optional roster note; default: none")
        if kind == "role":
            launch.add_argument(
                "--name",
                dest="entry_name",
                metavar="NAME",
                help="roster/session name; default: ROLE-<new session id's first 8 characters>",
            )
        launch.add_argument("key", metavar=key, help=f"required {key.lower()}; see below")
        launch.add_argument(
            "arguments",
            nargs=argparse.REMAINDER,
            metavar="DOWNSTREAM_OPTIONS",
            help=f"forwarded verbatim to {owner}; defaults/values: its --help",
        )
        launch.set_defaults(func=_launch, kind=kind, entry_name=None)
    attach = page(
        "attach",
        "Reconcile a roster and print recovery/monitoring commands",
        """Prerequisites and effects:
  Use the holder's environment when a holder is active. Does not acquire a
  lease or attach a terminal. Reconciles/adopts matching launches, shows
  eligible live panes and rotates events. Prints roster rows, backlog,
  recovery/monitor commands and N=cursor GEN=generation.
  --after and --gen go together; omit both to use the stored cursor.

Examples:
  agent-pm-crew attach --manager demo

Recovery and next help:
  Follow the printed monitor command to read events. Reading events alone
  does not verify notification delivery to a commander.
  On monitor expiry, use its printed monitor expired command, replacing LINE
  with the last consumed line; keep the matching generation.
  Acquire a holder: agent-pm-crew hold --help
  Event reading: agent-pm-crew next-event --help
""",
    )
    attach.add_argument("--after", type=_number, metavar="LINE", help="non-negative consumed line")
    attach.add_argument(
        "--gen", type=_number, metavar="GEN", help="non-negative matching generation"
    )
    attach.set_defaults(func=_attach)
    status = page(
        "status",
        "Print one Status line per selected roster entry",
        """Reads owning controllers; does not reconcile or acquire a holder.
Omit names for the whole roster. Partial operational failures exit 1 while
valid entries still print. Missing entries are usage errors.

Examples:
  agent-pm-crew status --manager demo

Recovery: agent-pm-crew attach --help
""",
    )
    status.add_argument(
        "names", nargs="*", metavar="NAME", help="entry names; default: all entries"
    )
    status.set_defaults(func=_status)
    report = page(
        "report",
        "Summarize every entry: state, note, latest reply and pull request",
        """Reads controllers and, for a pull request link, gh (authenticated).
Prints tab-separated rows including checks, mergeability and review. Does not
adopt, show, mark or acquire a lease; unavailable details are reported.

Examples:
  agent-pm-crew report --manager demo

Recovery: check gh authentication for missing pull request details;
agent-pm-crew status --help for individual status queries.
""",
    )
    report.set_defaults(func=_report)
    resume = page(
        "resume",
        "Recover gone entries or answer one waiting entry; print Records",
        """Requires existing roster Records and lease access. Live entries return
their Record; gone entries run their saved resume command. An entry already
resuming may print nothing. --input requires exactly one waiting entry;
without it, waiting entries are refused. Issue answers belong on the board.

Examples:
  With helper gone and its saved conversation available:
  agent-pm-crew resume --manager demo helper

Recovery: inspect state with agent-pm-crew status --help;
reconcile interrupted bookkeeping with agent-pm-crew attach --help.
""",
    )
    resume.add_argument("names", nargs="+", metavar="NAME", help="one or more existing entry names")
    resume.add_argument("--input", metavar="TEXT", help="answer exactly one waiting role entry")
    resume.set_defaults(func=_resume)
    for verb, text in (
        ("hide", "close each entry's panes; its work runs on"),
        ("show", "show each entry as at its launch; print attach commands its layer gives"),
    ):
        view = page(
            verb,
            text,
            f"""Give entry names or --all, never both; neither is a usage error.
Uses saved placement; no new --display is accepted. Work survives hide.
Requires the owning controllers and lease access; partial failures exit 1.

Examples:
  With a live helper entry:
  agent-pm-crew {verb} --manager demo helper

Recovery: show can print owning-layer attachment commands; run the printed
command when placement fails. Placement: agent-pm-session --help
""",
        )
        view.add_argument("names", nargs="*", metavar="NAME", help="existing roster entry names")
        view.add_argument("--all", action="store_true", help="every live entry")
        view.set_defaults(func=_view, verb=verb)
    for name, handler in (("close", _close), ("forget", _forget)):
        command = page(
            name,
            "Terminate an entry's work and remove its roster entry"
            if name == "close"
            else "Remove a stopped entry's bookkeeping",
            (
                "close calls the owning controller before removing the entry; a session\n"
                "name owned by an entry also selects it. Requires lease access.\n"
                if name == "close"
                else "By default refuses live work. --force skips controller checks and removes\n"
                "bookkeeping only; work may remain alive. Requires lease access.\n"
            )
            + f"\nExamples:\n  With helper {'tracked' if name == 'close' else 'stopped'}:\n"
            f"  agent-pm-crew {name} --manager demo helper\n"
            "\nRecovery: inspect entries with agent-pm-crew status --help\n",
        )
        command.add_argument("name", metavar="NAME", help="existing entry name")
        if name == "forget":
            command.add_argument(
                "--force",
                action="store_true",
                help="drop bookkeeping without ctl; work may remain alive",
            )
        command.set_defaults(func=handler)
    close_all = page(
        "close-all",
        "Terminate roster entries and the caller's descendant work",
        """Requires lease access and agent-pm-session listing. Also includes untracked
descendants opened by this caller. Prints selected names or nothing to close.
Without --dry-run closes work and removes successful entries; partial listing
or controller failures exit 1 and may leave work running.

Examples:
  From the intended commander's terminal, preview first:
  agent-pm-crew close-all --manager demo --dry-run

Recovery: agent-pm-crew attach --help
""",
    )
    close_all.add_argument(
        "--dry-run", action="store_true", help="list targets without closing/removing"
    )
    close_all.set_defaults(func=_close_all)
    note = page(
        "note",
        "Set or clear an entry's roster note",
        """Updates bookkeeping; prints nothing. Requires an existing entry and lease
access. Quote text containing spaces; empty text clears it.

Examples:
  With helper tracked:
  agent-pm-crew note --manager demo helper 'Waiting for review'
  agent-pm-crew note --manager demo helper ''

Inspect notes: agent-pm-crew report --help
""",
    )
    note.add_argument("name", metavar="NAME", help="existing entry name")
    note.add_argument("text", metavar="TEXT", help="required note; empty text clears it")
    note.set_defaults(func=_note)
    page(
        "release",
        "Release this holder's lease without closing its work",
        """Requires the current holder's environment. Removes ownership, not roster
entries or sessions; prints nothing. A holder normally releases on child exit.

Examples:
  Inside demo's current holder environment:
  agent-pm-crew release --manager demo

Acquire ownership again: agent-pm-crew hold --help
""",
    ).set_defaults(func=_release)
    next_event = page(
        "next-event",
        "Wait for and print roster events after a cursor",
        """Use paired N/GEN values printed by attach; --after end skips existing events.
Prints LINE EVENT, optionally followed by current owning-entry content.
Default: wait for one event. --follow repeats until stopped or an error.
Requires roster event access; no notification-delivery guarantee is implied.

Examples:
  If attach printed N=0 GEN=1 for demo:
  agent-pm-crew next-event --manager demo --after 0 --gen 1 --content --follow

Recovery: on expiry/rotation, use attach's printed monitor expired command
with the last consumed LINE and its GEN. Then use its new monitor command.
Cursor reconciliation: agent-pm-crew attach --help
""",
    )
    next_event.add_argument(
        "--after", type=_after, required=True, metavar="LINE|end", help="consumed line >=0, or end"
    )
    next_event.add_argument(
        "--gen", type=_number, required=True, metavar="GEN", help="matching generation >=0"
    )
    next_event.add_argument(
        "--content",
        action="store_true",
        help="then print the owning entry's ctl content at the time of reading",
    )
    next_event.add_argument(
        "--follow", action="store_true", help="keep printing events until stopped or failing"
    )
    next_event.set_defaults(func=_next_event)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = ()
    if arguments and arguments[0] in {"run", "hold"} and "--" in arguments:
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
    if args.func is _view and bool(args.names) == args.all:
        parser.error(f"{args.verb} takes entry names or --all")
    if args.func is _attach and (args.after is None) != (args.gen is None):
        parser.error("--after and --gen go together")
    if args.func is _resume and args.input is not None and len(args.names) != 1:
        parser.error("resume --input requires exactly one name")
    if args.func in {_run, _hold, _resume, _launch} and (refused := nested_start()):
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
