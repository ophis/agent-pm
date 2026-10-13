"""A manager's workers, each a plain tui session reached only through agent_pm.session, and its
roster (roster.py). start picks the runtime (--client, else the caller's own tui session's, else the
config's `runtime`, roster.default_runtime), starts the worker with its workdir
~/.agent-pm/workers/<sid>/, records it there in worker.json (what restart replays), shows it, enters
it in the roster and watches it EARLY s for a death or a dialog. attach syncs the roster with the
live sessions, reopens the unshown and prints the table, the backlog and how to arm a monitor. Exits
1 on an error, 2 on a usage error. Follow-ups go through the Driver's tmux_host.py send. Run as `env
PYTHONPATH=<the import roots>[:U] python3 -P -m agent_pm.manager.engine.crew …`, the form of each
crew command it prints. main first sets PYTHONPATH to U, the caller's minus each entry resolving to
one (unset when empty): what workers and recovered role or pipeline entries get; then configures the
session from the session config before a subcommand of STARTS (those that start, show or attach a
session); next-event configures just before it attaches, a config error becoming its content text.
The import roots (agent_pm.delegate's IMPORT_ROOTS), then U, appear only in a command running crew:
each one it prints (U as at print time) and a recovered worker entry.
"""

import argparse
import os
import re
import sys

from agent_pm.delegate import IMPORT_ROOTS
from agent_pm.manager.engine.controls import forget, note, recover, release, stop, stop_all
from agent_pm.manager.engine.crew_context import caller, configure, manager_directory, own_session
from agent_pm.manager.engine.monitor import attach, content, next_event, record_cursor
from agent_pm.manager.engine.types import (
    CommandArgumentError,
    Refused,
    Spec,
    WorkersError,
)
from agent_pm.manager.engine.workers import reply, restart, start

STARTS = ("start", "restart", "attach", "reply")


def _after(value: str) -> int | None:
    if value == "end":
        return None
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise CommandArgumentError.after(value)


def _number(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise CommandArgumentError.number(value)


def _drop_import_root() -> None:
    roots = {os.path.realpath(r) for r in IMPORT_ROOTS}
    entries = os.environ.pop("PYTHONPATH", "").split(os.pathsep)
    if user := os.pathsep.join(p for p in entries if os.path.realpath(p) not in roots):
        os.environ["PYTHONPATH"] = user


def _start_arguments(sub) -> None:
    p = sub.add_parser("start")
    p.add_argument("name")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p.add_argument("--cwd")
    p.add_argument("--prompt")
    p.add_argument("--resume", metavar="SID", help="a session id")
    p.add_argument(
        "--client",
        metavar="NAME",
        help="the worker's runtime (default: your tui session's, else the config's runtime)",
    )
    p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="sets a runtime-declared default in the worker's env; repeatable",
    )
    p.add_argument("--note", help="its roster note, as crew.py note sets it")
    p.add_argument("--split-from")
    p.add_argument("--split", choices=("right", "below"))


def _monitor_arguments(sub) -> None:
    p = sub.add_parser("next-event")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p.add_argument("--after", type=_after, required=True)
    p.add_argument("--gen", type=_number, required=True)
    p = sub.add_parser("attach")
    p.add_argument(
        "--manager", help="manager directory to attach to (default: your tmux session's)"
    )
    p.add_argument(
        "--after",
        type=_number,
        metavar="LINE",
        help="the line of the last event handled; with --gen",
    )
    p.add_argument(
        "--gen", type=_number, metavar="G", help="the generation of that line; with --after"
    )
    p.add_argument(
        "--resume", action="store_true", help="then run each gone entry's recovery command"
    )


def _roster_arguments(sub) -> None:
    p = sub.add_parser("release")
    p.add_argument("--manager", help="manager directory to release (default: your tmux session's)")
    p = sub.add_parser("note")
    p.add_argument("name")
    p.add_argument("text", help="'' clears it")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p = sub.add_parser("stop")
    p.add_argument("name", help="a roster entry, or its tui session")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p = sub.add_parser(
        "stop-all", help="stop every session your tmux session opened, and those they opened"
    )
    p.add_argument("--dry-run", action="store_true", help="print them, stop nothing")
    p = sub.add_parser("forget")
    p.add_argument("name")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="crew.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _start_arguments(sub)
    _monitor_arguments(sub)
    _roster_arguments(sub)
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
    return ap


def _attach(a, driver) -> int:
    text, entries = attach(
        manager_directory(a.manager, driver=driver),
        own_session(driver=driver),
        after=a.after,
        gen=a.gen,
        driver=driver,
    )
    print(text, end="", flush=True)
    failed = False
    if a.resume:
        for ok, line in recover(entries):
            print(line, flush=True)
            failed = failed or not ok
    return int(failed)


def _stop_all(a, driver) -> None:
    lines, left = stop_all(own_session(driver=driver), dry_run=a.dry_run, driver=driver)
    for line in lines or ([] if left else ["workers: stop-all: nothing to stop"]):
        print(line)
    if left:
        raise WorkersError.kill_failed(left)


def _roster_command(a, driver) -> int:
    if a.cmd == "attach":
        return _attach(a, driver)
    if a.cmd == "stop-all":
        _stop_all(a, driver)
        return 0
    directory = manager_directory(a.manager, driver=driver)
    own = own_session(driver=driver)
    if a.cmd == "release":
        release(directory, own, driver=driver)
        print(f"workers: released {directory}")
    elif a.cmd == "note":
        note(directory, own, a.name, a.text, driver=driver)
    elif a.cmd == "stop":
        stop(directory, own, a.name, driver=driver)
    else:
        forget(directory, own, a.name, driver=driver)
    return 0


def _worker_command(a, flags, driver) -> None:
    if a.cmd == "start":
        directory = manager_directory(a.manager, driver=driver)
        parent = caller(driver=driver)
        spec = Spec(
            a.name,
            a.cwd or os.getcwd(),
            prompt=a.prompt,
            note=a.note,
            flags=tuple(flags),
            client=a.client,
            overrides=tuple(a.env),
            resume=a.resume,
            split=a.split,
            split_from=a.split_from,
        )
        sid = start(
            spec,
            directory=directory,
            own=None if parent is None else parent.name,
            own_runtime=None if parent is None else parent.runtime,
            driver=driver,
        )
        print(f"{a.name} {sid}")
    elif a.cmd == "restart":
        restart(a.name, own=own_session(driver=driver), driver=driver)
    else:
        text = reply(a.name, driver=driver)
        if text:
            print(text)


def _next_event(a, driver, event_source) -> None:
    events, after, due = record_cursor(
        manager_directory(a.manager, driver=driver),
        own_session(driver=driver),
        a.after,
        a.gen,
        driver=driver,
    )
    if due:
        raise Refused.rotation_due()
    read_event = next_event if event_source is None else event_source.next
    line, event = read_event(events, after)
    print(f"{line} {event}")
    try:
        text = content(event, driver=driver)
    except WorkersError as e:
        text = f"workers: {e}"  # stdout: background readers watch only it
    if text:
        print(text)


def main(argv=None, *, driver=None, event_source=None) -> int:
    _drop_import_root()
    argv = sys.argv[1:] if argv is None else list(argv)
    flags = []
    if "--" in argv:
        i = argv.index("--")
        argv, flags = argv[:i], argv[i + 1 :]
    ap = _parser()
    a = ap.parse_args(argv)
    if a.cmd == "attach" and (a.after is None) != (a.gen is None):
        ap.error("--after and --gen go together")
    try:
        if a.cmd in STARTS:
            driver = configure() if driver is None else driver
        if a.cmd in ("start", "restart", "reply"):
            _worker_command(a, flags, driver)
        elif a.cmd == "next-event":
            _next_event(a, driver, event_source)
        else:
            return _roster_command(a, driver)
    except Refused as e:
        print(f"workers: {e}")
        return 1
    except WorkersError as e:
        print(f"workers: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
