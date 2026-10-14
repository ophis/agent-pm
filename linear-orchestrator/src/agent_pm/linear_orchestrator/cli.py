"""The Linear orchestrator's command entry point."""

import argparse
import sys
from datetime import UTC, datetime
from functools import partial

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.delegate import DriverError
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine import promote, router, run, runs
from agent_pm.linear_orchestrator.engine.config import load
from agent_pm.linear_orchestrator.engine.gate import Gate
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.types import ConfigError
from agent_pm.utils import Record, query_batch, query_inputs


def main(argv=None, *, cfg=None):
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
        query.add_argument("keys", nargs="*", metavar="ISSUE", help="omit or -: Records on stdin")
    args = parser.parse_args(arguments)
    try:
        inputs = query_inputs(args.keys, sys.stdin)
        cfg = load() if cfg is None else cfg
    except (ConfigError, DelegateConfigError, ValueError, OSError) as error:
        print(f"agent-pm-linear: {error}", file=sys.stderr)
        return 2
    event_log.use_logs(cfg.logs_dir)

    gate = None

    def control(item):
        nonlocal gate
        issue = item.name if isinstance(item, Record) else item
        sid = item.sid if isinstance(item, Record) else None
        view = LinearRun.attach(issue, cfg=cfg, sid=sid)
        if args.command == "status":
            return view.status().to_line()
        if args.command == "resume":
            if not view.alive() and view.outcome() is None:
                entries = runs.parse_log(cfg.runs_log)
                entry = runs.latest(entries)[issue]
                gate = gate or Gate(
                    cfg.runs_log,
                    datetime.now(UTC),
                    cfg=cfg,
                    dry=False,
                    say=partial(event_log.log, "router", quiet=True),
                )
                if gate.blocked(
                    entry.role,
                    issue,
                    key=(view.delegate_kind, runs.runtime_of(entries, view.sid)),
                ):
                    raise RuntimeError("resume blocked by usage gate")
            return view.resume().to_line()
        if args.command == "close":
            view.close()
            return None
        if args.command == "show":
            return view.show()
        command = view.attach_command()
        if command is None:
            raise RuntimeError("run has no live displayable session")
        return command

    return query_batch(
        inputs,
        control,
        configuration_errors=(ConfigError, DelegateConfigError, ValueError),
        operational_errors=(DriverError, RuntimeError, OSError),
    )


if __name__ == "__main__":
    sys.exit(main())
