"""The Linear orchestrator's command entry point."""

import argparse
import os
import sys
from datetime import UTC, datetime
from functools import partial

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.delegate import DelegateEvent, SessionError
from agent_pm.linear_orchestrator.engine import (
    client,
    issue,
    promote,
    prune,
    router,
    runs,
    writeback,
)
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.config import load
from agent_pm.linear_orchestrator.engine.gate import Gate
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.types import ConfigError
from agent_pm.utils import SID_ENV, Record, answer_keys, nested_start


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
            "run agent-pm-linear attach-command TASK-1 and run the printed command.\n"
            "Placement: agent-pm-session --help"
        ),
        "hide": "To reopen its saved view: agent-pm-linear show TASK-1",
        "close": "To recover interrupted work: agent-pm-linear resume TASK-1",
        "attach-command": (
            "Run the printed command yourself; this command does not attach. A headless\n"
            "or ended run may have no live showable session. Inspect it with\n"
            "agent-pm-linear status TASK-1; recovery: agent-pm-linear resume --help"
        ),
        "resume": (
            "Live or completed runs retain their identity; interrupted runs restart.\n"
            "A usage gate may block restart; wait for its reset before retrying.\n"
            "--input is rejected: put pipeline answers on the issue board, then resume.\n"
            "Run outside an agent session. AGENT_PM_SID, if supplied, must match\n"
            "the recorded session and is read once and removed.\n"
            "For a new board-driven attempt: agent-pm-linear router --help"
        ),
        "content": "To inspect liveness and outcome: agent-pm-linear status TASK-1",
    }
    count = (
        "Supply exactly one issue or one Record."
        if command in {"resume", "content"}
        else "Multiple issues/Records are handled in order; empty stdin does nothing."
    )
    return purposes[command], (
        "Prerequisites and effects:\n"
        "  Requires configured Linear/Delegate settings and an existing recorded run.\n"
        "  ISSUE selects its latest run; a Record also verifies its recorded session.\n"
        "  Explicit ISSUE arguments leave stdin unread; otherwise read one Record\n"
        "  JSON line per stdin line. Session names and workdirs are not issue keys.\n"
        f"  {count}\n"
        "  A duplicate issue or malformed Record is a usage error. Status, content,\n"
        "  resume and attach-command print to stdout; show/hide/close are silent.\n"
        "  Exit 0: success; 1: operation failed; 2: usage/configuration error.\n"
        "  Batch failures retain other valid results; errors go to stderr.\n\n"
        "Example (TASK-1 has a configured recorded run):\n"
        f"  agent-pm-linear {command} TASK-1\n\n"
        f"Recovery and next help:\n{details[command]}\n"
        "Configuration and command discovery: agent-pm-linear --help\n"
        "Record schema: agent-pm-session --help"
    )


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    # each parses its own options and --help
    entries = {
        "router": (router.main, "claim or resume an issue's agent run: a tick, --issue or --brake"),
        "promote": (promote.main, "promote Handoff issues to the next role, then prune"),
        "prune": (prune.main, "close finished issues' runs, delete their agent dirs, then archive"),
        "issue": (
            issue.main,
            "read, move, comment on or archive one Linear issue, list issues, look up references",
        ),
    }
    if arguments and arguments[0] in entries:
        return entries[arguments[0]][0](arguments[1:])
    launcher_sid = os.environ.pop(SID_ENV, "") or None
    parser = argparse.ArgumentParser(
        prog="agent-pm-linear",
        description="Route issue work to agents and control their recorded pipeline runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Prerequisites and effects:\n"
            "  Operational commands require configured linear-orchestrator and Delegate\n"
            "  settings. Board access uses the configured harness_key Keychain service;\n"
            "  role keys identify the accounts used for issue work. Python 3.11+ is needed.\n"
            "  Router launches need the configured client/Delegate and its Host tools.\n"
            "  Router/promote/prune prepend AGENT_PM_PATH, then this installation's bin,\n"
            "  then default executable directories. Logs: work_dir/logs/orchestrator.jsonl;\n"
            "  run identities: work_dir/logs/runs.jsonl. Help needs no credentials/services.\n"
            "  Query ISSUE values are identifiers such as TASK-1, not session names.\n"
            "  Omit them to read Records from stdin; Record schema: agent-pm-session --help\n\n"
            "Example (configured Linear access and TASK-1):\n"
            "  agent-pm-linear router --issue TASK-1 --dry-run\n"
            "  Prints a plan without claiming or launching the issue.\n\n"
            "Recovery and next help:\n"
            "  Start here: agent-pm-linear --help\n"
            "  Command details: agent-pm-linear router --help\n"
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
            "  Requires configured Linear access, the run's existing start row and matching\n"
            "  issue/workdir/session identity. Read exactly one DelegateEvent JSON line\n"
            "  from stdin through EOF, not a Record or an event stream. The Delegate harness\n"
            "  supplies this callback; it may post comments, publish outcomes, move issue\n"
            "  state and update local delivery records. No normal stdout output.\n"
            "  --issue and --workdir are required; repeated values use the last value.\n"
            "  Exit 0: delivered/already handled; 1: delivery incomplete; 2: invalid event,\n"
            "  identity or configuration. Errors go to stderr. Let the harness retry an\n"
            "  incomplete delivery; fix the reported identity/input error first.\n\n"
            "Example invocation form (automation supplies ISSUE, WORKDIR and EVENT_FILE):\n"
            "  agent-pm-linear writeback --issue ISSUE --workdir WORKDIR < EVENT_FILE\n\n"
            "Recovery and next help:\n"
            "  Configuration and run discovery: agent-pm-linear --help\n"
            "  Inspect the matching run: agent-pm-linear status --help"
        ),
    )
    back.add_argument("--issue", required=True, metavar="ID", help="owning issue identifier")
    back.add_argument(
        "--workdir", required=True, metavar="DIR", help="owning run's recorded work directory"
    )
    args = parser.parse_args(arguments)
    if args.command == "resume" and args.input is not None:
        print("agent-pm-linear: pipeline answers belong on the issue board", file=sys.stderr)
        return 2
    if args.command == "resume" and (refused := nested_start()):
        print(f"agent-pm-linear: {refused}", file=sys.stderr)
        return 2
    try:
        cfg = load()
    except (ConfigError, DelegateConfigError, ValueError, OSError) as error:
        print(f"agent-pm-linear: {error}", file=sys.stderr)
        return 2
    event_log.use_logs(cfg.logs_dir)
    if args.command == "writeback":
        return _writeback(cfg, args.issue, args.workdir)

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
        prog="agent-pm-linear",
        out=sys.stdout,
        err=sys.stderr,
    )


def _writeback(cfg, issue, workdir):
    lines = sys.stdin.read().splitlines()
    try:
        if len(lines) != 1:
            raise ValueError(f"want one DelegateEvent line on stdin, got {len(lines)}")
        event = DelegateEvent.parse(lines[0])
        return 0 if writeback.deliver(cfg, issue, workdir, event, gql=client.linear_gql) else 1
    except (ValueError, ConfigError) as error:
        print(f"agent-pm-linear writeback: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
