"""The orchestrator's command entry point."""

import argparse
import contextlib
import os
import sys
from datetime import UTC, datetime
from functools import partial
from types import MappingProxyType

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.delegate import DelegateEvent, SessionError
from agent_pm.orchestrator.engine import log as event_log
from agent_pm.orchestrator.engine import (
    promote,
    prune,
    router,
    runs,
    writeback,
)
from agent_pm.orchestrator.engine.config import linear_settings, load
from agent_pm.orchestrator.engine.gate import Gate
from agent_pm.orchestrator.engine.linear_run import LinearRun
from agent_pm.orchestrator.engine.types import ConfigError, TrackerKind
from agent_pm.orchestrator.trackers.linear import (
    client,
    issue,
)
from agent_pm.orchestrator.trackers.linear.linear_tracker import LinearTracker
from agent_pm.orchestrator.trackers.local.local_tracker import LocalTracker
from agent_pm.utils import (
    SID_ENV,
    Record,
    answer_keys,
    nested_start,
    one_line,
    tolerate_unknown_keys,
)


def _query_help(command):
    purposes = {
        "status": "Print one read-only Status JSON line per issue's latest recorded run.",
        "show": "Open each run's view at its saved display; work keeps running.",
        "hide": "Hide each run's view while its work continues.",
        "close": "Terminate each selected run; keep its files and recorded identity.",
        "attach-command": "Print one command per live showable run for manual attachment.",
        "resume": "Recover exactly one recorded run and print its Record JSON line.",
        "content": "Print exactly one recorded run's current content to stdout.",
    }
    details = {
        "status": "Use status before deciding whether to show, resume or close a run.",
        "show": (
            "Uses the saved display; no new --display is accepted. If no view opens,\n"
            "run agent-pm-orchestrator attach-command TASK-1 and run the printed command.\n"
            "Placement: agent-pm-session --help"
        ),
        "hide": "To reopen its saved view: agent-pm-orchestrator show TASK-1",
        "close": "To recover interrupted work: agent-pm-orchestrator resume TASK-1",
        "attach-command": (
            "Run the printed command yourself; this command does not attach. A headless\n"
            "or ended run may have no live showable session. Inspect it with\n"
            "agent-pm-orchestrator status TASK-1; recovery: agent-pm-orchestrator resume --help"
        ),
        "resume": (
            "Live or completed runs retain their identity; interrupted runs restart.\n"
            "A usage gate may block restart; wait for its reset before retrying.\n"
            "--input is rejected: put pipeline answers on the issue board, then resume.\n"
            "Run outside an agent session. AGENT_PM_SID, if supplied, must match\n"
            "the recorded session and is read once and removed.\n"
            'Needs [orchestrator] tracker = "linear"; a local board refuses it.\n'
            "For a new board-driven attempt: agent-pm-orchestrator router --help"
        ),
        "content": "To inspect liveness and outcome: agent-pm-orchestrator status TASK-1",
    }
    count = (
        "Supply exactly one issue or one Record."
        if command in {"resume", "content"}
        else "Multiple issues/Records are handled in order; empty stdin does nothing."
    )
    return purposes[command], (
        "Prerequisites and effects:\n"
        "  Requires configured tracker/Delegate settings and an existing recorded run.\n"
        "  ISSUE selects its latest run; a Record also verifies its recorded session.\n"
        "  Explicit ISSUE arguments leave stdin unread; otherwise read one Record\n"
        "  JSON line per stdin line. Session names and workdirs are not issue keys.\n"
        f"  {count}\n"
        "  A duplicate issue or malformed Record is a usage error. Status, content,\n"
        "  resume and attach-command print to stdout; show/hide/close are silent.\n"
        "  Exit 0: success; 1: operation failed; 2: usage/configuration error.\n"
        "  Batch failures retain other valid results; errors go to stderr.\n\n"
        "Example (TASK-1 has a configured recorded run):\n"
        f"  agent-pm-orchestrator {command} TASK-1\n\n"
        f"Recovery and next help:\n{details[command]}\n"
        "Configuration and command discovery: agent-pm-orchestrator --help\n"
        "Record schema: agent-pm-session --help"
    )


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    # each parses its own options and --help
    entries = {
        "router": (
            partial(router.main, trackers=TRACKERS),
            "claim or resume an issue's agent run: a tick, --issue or --brake",
        ),
        "promote": (promote.main, "promote Handoff issues to the next role, then prune"),
        "prune": (prune.main, "close finished issues' runs, delete their agent dirs, then archive"),
        "issue": (
            issue.main,
            "read, move, comment on, archive, relate or relabel one Linear issue, list issues, "
            "look up references",
        ),
    }
    if arguments and arguments[0] in entries:
        return entries[arguments[0]][0](arguments[1:])
    launcher_sid = os.environ.pop(SID_ENV, "") or None
    parser = argparse.ArgumentParser(
        prog="agent-pm-orchestrator",
        description="Route issue work to agents and control their recorded pipeline runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Prerequisites and effects:\n"
            "  Operational commands require configured orchestrator and Delegate\n"
            '  settings. [orchestrator] tracker picks the board: "local" (the default)\n'
            "  reads the directory $AGENT_PM_BOARD_DIR (default ~/.agent-pm/board), where\n"
            '  only router --dry-run and --brake run yet; "linear" reaches Linear through\n'
            "  the linear.harness_key Keychain service (the issue commands:\n"
            "  linear.manager_key), role keys identifying the accounts used for issue\n"
            "  work. Python 3.11+ is needed.\n"
            "  Router launches need the configured client/Delegate and its Host tools.\n"
            "  Router/promote/prune prepend AGENT_PM_PATH, then this installation's bin,\n"
            "  then default executable directories. Logs: STATE/logs/orchestrator.jsonl;\n"
            "  run identities: STATE/logs/runs.jsonl; STATE is work_dir on Linear and\n"
            "  <board>/.state on a local board. Help needs no credentials/services.\n"
            "  Query ISSUE values are identifiers such as TASK-1, not session names.\n"
            "  Omit them to read Records from stdin; Record schema: agent-pm-session --help\n\n"
            "Example (a configured tracker holding TASK-1):\n"
            "  agent-pm-orchestrator router --issue TASK-1 --dry-run\n"
            "  Prints a plan without claiming or launching the issue.\n\n"
            "Recovery and next help:\n"
            "  Start here: agent-pm-orchestrator --help\n"
            "  Command details: agent-pm-orchestrator router --help\n"
            "  Fix missing settings/Keychain services named in errors before retrying.\n"
            "  Role/task discovery: agent-pm-delegate run --list\n"
            "  Placement: agent-pm-session --help"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command, (_, text) in entries.items():
        commands.add_parser(command, help=text)
    for command in ("status", "show", "hide", "close", "attach-command", "resume", "content"):
        purpose, guidance = _query_help(command)
        query = commands.add_parser(
            command,
            help=purpose,
            description=purpose,
            epilog=guidance,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        if command == "resume":
            query.add_argument(
                "--input", metavar="TEXT", help="unsupported; put answers on the issue board"
            )
        query.add_argument(
            "keys", nargs="*", metavar="ISSUE", help="issue identifiers; omit: Records on stdin"
        )
    back = commands.add_parser(
        "writeback",
        help="automation: deliver one DelegateEvent from stdin to its issue",
        description="Automation callback: write one DelegateEvent line to its issue board.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Prerequisites and effects:\n"
            '  Requires tracker = "linear", configured Linear access, the run\'s existing\n'
            "  start row and matching issue/workdir/session identity. Read exactly one\n"
            "  DelegateEvent JSON line from stdin through EOF, not a Record or an event\n"
            "  stream. The Delegate harness supplies this callback; it may post comments,\n"
            "  publish outcomes, move issue state and update local delivery records. No\n"
            "  normal stdout output.\n"
            "  --issue and --workdir are required; repeated values use the last value.\n"
            "  Exit 0: delivered/already handled; 1: delivery incomplete; 2: invalid event,\n"
            "  identity or configuration. Errors go to stderr. Let the harness retry an\n"
            "  incomplete delivery; fix the reported identity/input error first.\n\n"
            "Example invocation form (automation supplies ISSUE, WORKDIR and EVENT_FILE):\n"
            "  agent-pm-orchestrator writeback --issue ISSUE --workdir WORKDIR < EVENT_FILE\n\n"
            "Recovery and next help:\n"
            "  Configuration and run discovery: agent-pm-orchestrator --help\n"
            "  Inspect the matching run: agent-pm-orchestrator status --help"
        ),
    )
    back.add_argument("--issue", required=True, metavar="ID", help="owning issue identifier")
    back.add_argument(
        "--workdir", required=True, metavar="DIR", help="owning run's recorded work directory"
    )
    args = parser.parse_args(arguments)
    if args.command == "resume" and args.input is not None:
        print("agent-pm-orchestrator: pipeline answers belong on the issue board", file=sys.stderr)
        return 2
    if args.command == "resume" and (refused := nested_start()):
        print(f"agent-pm-orchestrator: {refused}", file=sys.stderr)
        return 2
    ignored = []
    # writeback ignores config keys newer than this code: a run begun before an upgrade still writes back
    tolerated = (
        tolerate_unknown_keys(partial(_ignored, ignored))
        if args.command == "writeback"
        else contextlib.nullcontext()
    )
    with tolerated:
        try:
            cfg = load()
        except (ConfigError, DelegateConfigError, ValueError, OSError) as error:
            print(f"agent-pm-orchestrator: {error}", file=sys.stderr)
            logs = getattr(error, "logs_dir", None)
            if args.command == "writeback" and logs:
                event_log.use_logs(logs)
                _refused(args.issue, error)
                _log_ignored(args.issue, ignored)
            return 2
        event_log.use_logs(cfg.logs_dir)
        if args.command in {"writeback", "resume"}:
            try:
                linear = linear_settings(cfg, args.command)
            except ConfigError as refused:
                print(f"agent-pm-orchestrator: {refused}", file=sys.stderr)
                if args.command == "writeback":
                    _refused(args.issue, refused)
                    _log_ignored(args.issue, ignored)
                return 2
            if args.command == "writeback":
                try:
                    return _writeback(cfg, linear, args.issue, args.workdir)
                finally:
                    _log_ignored(args.issue, ignored)

    def viewed(key):
        issue = key.name if isinstance(key, Record) else key
        sid = key.sid if isinstance(key, Record) else None
        view = LinearRun.attach(issue, cfg=cfg, sid=sid)
        if args.command == "resume" and launcher_sid is not None and launcher_sid != view.sid:
            raise ConfigError(f"${SID_ENV} {launcher_sid} differs from run sid {view.sid}")
        return issue, view

    def resume(view):
        if not view.delegate.alive() and view.delegate.outcome() is None:
            entries = runs.parse_log(cfg.runs_log)
            entry = runs.latest(entries)[view.issue]
            gate = Gate(
                cfg.runs_log,
                datetime.now(UTC),
                cfg=cfg,
                dry=False,
                say=partial(event_log.log, "router", quiet=True),
            )
            if gate.blocked(
                entry.role,
                view.issue,
                key=(view.delegate_kind, runs.client_of(entries, view.sid)),
            ):
                raise RuntimeError("resume blocked by usage gate")
        return view.resume().to_line()

    def attach_command(view):
        command = view.delegate.attach_command()
        if command is None:
            raise RuntimeError("run has no live showable session")
        return command

    reply = {
        "status": lambda view: view.status().to_line(),
        "content": lambda view: view.content(),
        "resume": resume,
        "close": lambda view: view.delegate.close(),
        "show": lambda view: view.delegate.show(),
        "hide": lambda view: view.delegate.hide(),
        "attach-command": attach_command,
    }[args.command]
    return answer_keys(
        args.keys,
        sys.stdin,
        viewed,
        reply,
        single=args.command in {"resume", "content"},
        usage=(ConfigError, DelegateConfigError, ValueError),
        failures=(SessionError, RuntimeError, OSError),
        prog="agent-pm-orchestrator",
        out=sys.stdout,
        err=sys.stderr,
    )


def _local_board(transport, cfg, *, say):
    """The Connect for tracker = "local": a board read needs no transport and logs nothing."""
    del transport, say
    return LocalTracker(cfg.board, cfg.roles)


TRACKERS = MappingProxyType(
    {TrackerKind.LINEAR: LinearTracker.connect, TrackerKind.LOCAL: _local_board}
)


def _ignored(ignored, error):
    print(f"agent-pm-orchestrator writeback: ignored: {error}", file=sys.stderr)
    ignored.append(error)


def _log_ignored(issue, ignored):
    for error in ignored:
        event_log.log("writeback", "config-unknown", issue, quiet=True, reason=error)
    ignored.clear()


def _writeback(cfg, linear, issue, workdir):
    lines = sys.stdin.read().splitlines()
    try:
        if len(lines) != 1:
            raise ValueError(f"want one DelegateEvent line on stdin, got {len(lines)}")
        event = DelegateEvent.parse(lines[0])
        return (
            0 if writeback.deliver(cfg, linear, issue, workdir, event, gql=client.linear_gql) else 1
        )
    except (ValueError, ConfigError) as error:
        print(f"agent-pm-orchestrator writeback: {error}", file=sys.stderr)
        _refused(issue, error)
        return 2


def _refused(issue, error):
    # the harness keeps a failed callback's exit code; the log keeps why
    event_log.log("writeback", "refused", issue, quiet=True, reason=one_line(str(error)), exit=2)


if __name__ == "__main__":
    sys.exit(main())
