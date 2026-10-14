"""The Linear orchestrator's command entry point."""

import argparse
import sys
from datetime import UTC, datetime
from functools import partial

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.delegate import DelegateEvent, SessionError
from agent_pm.linear_orchestrator.engine import client, promote, router, run, runs, writeback
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.config import load
from agent_pm.linear_orchestrator.engine.gate import Gate
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.types import ConfigError
from agent_pm.utils import Record, answer_keys


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    legacy = {"router": router.main, "promote": promote.main, "run": run.main}
    if arguments and arguments[0] in legacy:
        return legacy[arguments[0]](arguments[1:])
    parser = argparse.ArgumentParser(prog="agent-pm-linear")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in legacy:
        commands.add_parser(command, help="legacy entry, replaced by TASK-374")
    for command in ("status", "show", "close", "attach-command", "resume"):
        query = commands.add_parser(command)
        query.add_argument("keys", nargs="*", metavar="ISSUE", help="omit: Records on stdin")
    back = commands.add_parser("writeback", help="write back one DelegateEvent line from stdin")
    back.add_argument("--issue", required=True)
    back.add_argument("--workdir", required=True)
    args = parser.parse_args(arguments)
    try:
        cfg = load()
    except (ConfigError, DelegateConfigError, ValueError, OSError) as error:
        print(f"agent-pm-linear: {error}", file=sys.stderr)
        return 2
    event_log.use_logs(cfg.logs_dir)
    if args.command == "writeback":
        return _writeback(cfg, args.issue, args.workdir)

    gate = None

    def viewed(key):
        issue = key.name if isinstance(key, Record) else key
        sid = key.sid if isinstance(key, Record) else None
        return issue, LinearRun.attach(issue, cfg=cfg, sid=sid)

    def resume(view):
        nonlocal gate
        if not view.alive() and view.outcome() is None:
            entries = runs.parse_log(cfg.runs_log)
            entry = runs.latest(entries)[view.issue]
            gate = gate or Gate(
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
        command = view.attach_command()
        if command is None:
            raise RuntimeError("run has no live displayable session")
        return command

    reply = {
        "status": lambda view: view.status().to_line(),
        "resume": resume,
        "close": lambda view: view.close(),
        "show": lambda view: view.show(),
        "attach-command": attach_command,
    }[args.command]
    return answer_keys(
        args.keys,
        sys.stdin,
        viewed,
        reply,
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
