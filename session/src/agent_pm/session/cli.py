"""agent-pm-session, the session layer's command (FR-31, FR-42; bin/agent-pm-session runs it).

start, restart and resume a worker (engine/worker.py) and print its Record; reply, send (text, or
keys in KeyPress notation) and read a session; status, show, close and attach-command for session
names, or Records one per stdin line; ls; tile; hook, a tui client's hook call (tui_hook.run).
Every subcommand but hook reads the session config once: read_config, Host.parse_hosts,
Session.configure. AGENT_PM_SID, read once here, is a start's or resume's session id.

stdout holds only machine output: Records, Status lines, ls rows, read and reply text, attach
commands. Exit 0 done, 2 a usage or config error (nothing done), 1 anything else. A batch answers
its keys in order; a failed key gets one stderr line and no stdout line, and the exit is 1.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections.abc import Iterator

import agent_pm.session.engine.types
import agent_pm.utils
from agent_pm.session.engine.base import Session
from agent_pm.session.engine.config import read_config
from agent_pm.session.engine.types import (
    COMMAND,
    TAG,
    ActivityState,
    ConfigError,
    Hosts,
    KeyPress,
    NotDisplayable,
    SessionError,
    SessionInfo,
    SessionRow,
)
from agent_pm.session.engine.worker import Worker
from agent_pm.session.hosts.base import Displayable, Host
from agent_pm.session.runtimes import tui_hook
from agent_pm.session.runtimes.base import EVENTS, START_SID
from agent_pm.session.runtimes.tui import TuiRuntime
from agent_pm.utils import Record, Status, module_roots

PROG = COMMAND
GONE = "gone"
HEADER = "name\tstate\tclient\tsid\tdisplay"


class UsageError(ConfigError):
    """A malformed request: exit 2, nothing done."""


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _name(value: str) -> str:
    if not TAG.fullmatch(value):
        raise argparse.ArgumentTypeError(f"want [A-Za-z0-9_-]+, not {value!r:.80}")
    return value


def _lines(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"want a positive int, not {value!r:.80}")
    return int(value)


def _env(item: str) -> tuple[str, str]:
    key, eq, value = item.partition("=")
    if not (eq and key) or "\0" in key or not value.isprintable():
        raise argparse.ArgumentTypeError(f"want NAME=VALUE, VALUE printable, not {item!r:.80}")
    return key, value


def _display_help(hosts: Hosts | None) -> str:
    """Each configured Host's display_help; a Host that cannot load fails its command later."""
    try:
        if hosts is not None:
            found = [f"  {h}: {Session.host(h).display_help() or 'as given'}" for h in hosts.tables]
            return "display (--display D), per configured Host:\n" + "\n".join(found)
    except ConfigError:
        pass
    return "display (--display D): the session config could not be read"


def _parser(hosts: Hosts | None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Start and reach agent sessions: workers (plain tui sessions by name),\n"
        "and any session for status, show, close, attach-command, ls, send, read and tile.",
        epilog=_display_help(hosts),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = sub.add_parser(
        "start",
        help="start worker NAME; print its Record",
        description="Start worker NAME, a plain tui session of CLIENT; everything after -- is "
        "its client's flags, verbatim. Its session id: --resume SID (resumes that "
        "conversation), else $AGENT_PM_SID, else a new one; the two must agree. Prints its "
        "Record once it has run 5 s without exiting (up to 15 s while its launch input is "
        "pending).",
    )
    p.add_argument("name", type=_name)
    p.add_argument("--client", required=True, help="its client, an [adapters.<name>]")
    p.add_argument("--cwd", help="its directory (default: this one)")
    p.add_argument("--resume", metavar="SID", help="resume conversation SID")
    p.add_argument("--prompt", default="", help="its first prompt")
    p.add_argument(
        "--env",
        action="append",
        default=[],
        type=_env,
        metavar="NAME=VALUE",
        help="set in its environment; repeatable, the last of a NAME wins",
    )
    p.add_argument("--display", metavar="D", help="where its Host shows it (see below)")
    for cmd, text in (
        ("restart", "restart a worker's session in place; print its Record"),
        ("resume", "print a live worker's Record, else resume it; print its Record"),
        ("reply", "print a tui session's latest reply"),
    ):
        sub.add_parser(cmd, help=text, description=text).add_argument("name", type=_name)
    for cmd, text in (
        ("status", "one Status JSON line per session"),
        ("show", "show each session where its display says"),
        ("close", "close each session; absent is fine"),
        ("attach-command", "print each session's attach command"),
    ):
        p = sub.add_parser(cmd, help=text, description=f"{text}: names, else Records on stdin.")
        p.add_argument("names", nargs="*", type=_name, metavar="NAME")
    p = sub.add_parser(
        "ls",
        help="list sessions",
        description="List sessions (see README.md › agent-pm-session) or, with --records, the "
        "Record of each that has one.",
    )
    p.add_argument("--match", metavar="RE", help="names fullmatching RE")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--display", metavar="D", help="those each Host matches to display D")
    where.add_argument("--here", action="store_true", help="those this caller opened")
    p.add_argument("--records", action="store_true", help="print their Records")
    p.add_argument(
        "--descendants",
        action="store_true",
        help="with --here --records: also those they opened, transitively (stop-all)",
    )
    p = sub.add_parser(
        "send",
        help="send TEXT (then Enter), or keys",
        description="Send TEXT as a paste then Enter, or --key K…: keys in a Host-independent "
        "notation: one character, or Enter Escape Tab BTab BSpace Up Down Left Right Home End "
        "PageUp PageDown F1-F12, after any of C- M- S- (C-c, S-Up).",
    )
    p.add_argument("name", type=_name)
    p.add_argument("text", nargs="?")
    p.add_argument("--key", nargs="+", metavar="K", help="keys, all checked before any is sent")
    p = sub.add_parser("read", help="print a session's screen")
    p.add_argument("name", type=_name)
    p.add_argument("--lines", type=_lines, metavar="N", help="the last N lines, with history")
    p = sub.add_parser("tile", help="lay out a window's sessions (the default Host's)")
    p.add_argument("window", help="the window, as its Host names it")
    p.add_argument(
        "--no-wait", action="store_true", help="return at once while another tile runs (hooks)"
    )
    p.add_argument(
        "--if-panes-changed",
        action="store_true",
        help="only when the window's panes changed since its last tile (hooks)",
    )
    sub.add_parser(
        "hook",
        help="a tui client's hook call (its hook config runs it)",
        description="DIR LABEL, the hook's payload on stdin; never fails, never prints stdout.",
    )
    p = sub.add_parser(
        "client-resume",
        help="resume a client's own session (its session info's resume command runs this)",
    )
    p.add_argument("client")
    p.add_argument("arguments", nargs=argparse.REMAINDER)
    return ap


def _configure() -> Hosts:
    config = read_config()
    hosts = Host.parse_hosts(config.get("hosts"))
    Session.configure_from(config, hosts=hosts)
    return hosts


def _host_of(name: str, hosts: Hosts) -> str:
    """The configured Host listing session `name`, else the default."""
    return next((h for h in hosts.tables if name in Session.host(h).list()), hosts.default)


def _shown(name: str, hosts: Hosts) -> Displayable:
    host = Session.host(_host_of(name, hosts))
    if not isinstance(host, Displayable):
        raise NotDisplayable(f"session {name}: host {host.name} cannot display")
    return host


def _keys(args) -> list[tuple[str, Record | None]]:
    found: list[tuple[str, Record | None]]
    if args.names:
        found = [(name, None) for name in args.names]
    elif sys.stdin is None or sys.stdin.isatty():
        raise UsageError(f"{args.cmd}: give session names or Records on stdin")
    else:
        found = []
        try:
            for line in sys.stdin:
                if line.strip():
                    record = Record.parse(line)
                    found.append((record.name, record))
        except UnicodeDecodeError:
            raise UsageError(f"{args.cmd}: stdin: not UTF-8") from None
        except ValueError as error:
            raise UsageError(f"{args.cmd}: stdin: {error}") from None
    names = [name for name, _ in found]
    if twice := sorted({name for name in names if names.count(name) > 1}):
        raise UsageError(f"{args.cmd}: {', '.join(twice)} given twice")
    for name, record in found:
        if not _sessions(name, record):
            raise UsageError(f"{name}: names no session")
        for session in _sessions(name, record):
            if not TAG.fullmatch(session):
                raise UsageError(f"{name}: session {session!r:.80}: want [A-Za-z0-9_-]+")
    return found


def _sessions(name: str, record: Record | None) -> tuple[str, ...]:
    return (name,) if record is None else record.sessions


def _state(name: str, hosts: Hosts) -> tuple[bool, str, str | None]:
    host = Session.host(_host_of(name, hosts))
    if name not in host.list():
        return False, GONE, None
    handle = host.attach(name)
    if not handle.alive():
        return False, ActivityState.DEAD, None
    attach = host.attach_command(name) if isinstance(host, Displayable) else None
    return True, handle.activity() or ActivityState.WORKING, attach


def _cwd(name: str) -> str | None:
    """Tui session `name`'s cwd as its start recorded it; None for any other session."""
    try:
        info = SessionInfo.from_fields(TuiRuntime.snapshot(name))
    except SessionError:
        return None
    return None if info is None else info.cwd


def _status(name: str, record: Record | None, hosts: Hosts) -> str:
    sessions = _sessions(name, record)
    found = [(session, *_state(session, hosts)) for session in sessions]
    session, alive, state, attach = next((each for each in found if each[1]), found[0])
    return Status(name, alive, str(state), None, attach, _cwd(session) if alive else None).to_line()


def _carried(name: str, record: Record | None, hosts: Hosts) -> tuple[str, ...]:
    """The key's sessions, once each live one publishes a Record key's sid: a reused name is
    another run's session.
    """
    sessions = _sessions(name, record)
    if record is None:
        return sessions
    for session in sessions:
        host = Session.host(_host_of(session, hosts))
        if session not in host.list():
            continue
        found = host.attach(session).record()
        have = None if found is None else found.sid
        if found is None or have != record.sid:
            raise SessionError(
                f"session {session} carries sid {have or 'none'}, not the Record's {record.sid}"
            )
    return sessions


def _show(name: str, record: Record | None, hosts: Hosts) -> None:
    for session in _carried(name, record, hosts):
        shown = _shown(session, hosts)
        if record is None:
            display = Session.host(_host_of(session, hosts)).attach(session).display()
        else:
            display = record.display
        command = shown.show(session, display=display)
        if command is not None:
            _say(f"session {session}: not shown; attach with {command}")


def _attach_command(name: str, record: Record | None, hosts: Hosts) -> str:
    session = _sessions(name, record)[0]
    return _shown(session, hosts).attach_command(session)


def _close(name: str, record: Record | None, hosts: Hosts) -> None:
    for session in _carried(name, record, hosts):
        Session.retire(session, host=_host_of(session, hosts))


BATCH = {"status": _status, "show": _show, "close": _close, "attach-command": _attach_command}


def _batch(args, hosts: Hosts) -> int:
    failed = 0
    for name, record in _keys(args):
        try:
            line = BATCH[args.cmd](name, record, hosts)
            if line is not None:
                print(line, flush=True)
        except NotDisplayable as error:
            _say(str(error))
            failed = 1
        except (ConfigError, SessionError, OSError) as error:
            _say(f"{PROG}: {name}: {error}")
            failed = 1
    return failed


def _listed(args, hosts: Hosts) -> Iterator[tuple[Host, str]]:
    try:
        pattern = None if args.match is None else re.compile(args.match)
    except re.error as error:
        raise UsageError(f"--match {args.match!r:.80}: {error}") from None
    for name in hosts.tables:
        host = Session.host(name)
        display = Session.display(None, host=name) if args.here else args.display
        if args.here and not display:
            continue
        names = host.descendants(display) if args.descendants else host.list(display)
        yield from ((host, n) for n in names if pattern is None or pattern.fullmatch(n))


def _row(row: SessionRow) -> str:
    values = (row.name, row.state, row.client, row.sid, row.display)
    return "\t".join("-" if value is None else str(value) for value in values)


def _ls(args, hosts: Hosts) -> int:
    if args.descendants and not (args.here and args.records):
        raise UsageError("ls: --descendants needs --here --records")
    if args.records:
        for host, name in sorted(_listed(args, hosts), key=lambda found: found[1]):
            try:
                record = host.attach(name).record()
            except SessionError:
                if name in host.list():
                    raise
                continue
            if record is not None:
                print(record.to_line())
        return 0
    if args.here:
        names = {name for _, name in _listed(args, hosts)}
        rows = [row for row in Session.list(args.match) if row.name in names]
    else:
        rows = Session.list(args.match, display=args.display)
    print(HEADER)
    for row in rows:
        print(_row(row))
    return 0


def _start(args, flags: list[str], hosts: Hosts, start_sid: str | None) -> int:
    if args.resume is not None and start_sid is not None and start_sid != args.resume:
        raise UsageError(f"{START_SID} {start_sid} differs from --resume {args.resume}")
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        raise UsageError(f"--cwd {cwd}: not a directory")
    events = os.environ.get(EVENTS) or None
    worker = Worker.new(
        name=args.name,
        sid=args.resume or start_sid or str(uuid.uuid4()),
        client=args.client,
        cwd=cwd,
        host=hosts.default,
        flags=tuple(flags),
        env=dict(args.env),
        events=None if events is None else os.path.abspath(events),
        display=Session.display(args.display, host=hosts.default),
    )
    worker.check(prompt=args.prompt, resume=args.resume is not None)
    with worker.locked():
        record = worker.start(prompt=args.prompt, resume=args.resume is not None)
    print(record.to_line())
    return 0


def _worker(args, start_sid: str | None) -> int:
    worker = Worker.load(args.name, sid=start_sid)
    with worker.locked():
        record = worker.restart() if args.cmd == "restart" else worker.resume()
    print(record.to_line())
    return 0


def _send(args, hosts: Hosts) -> int:
    if (args.text is None) == (args.key is None):
        raise UsageError("send: give TEXT or --key K…, not both")
    keys = None if args.key is None else tuple(map(KeyPress.parse, args.key))
    session = Session.attach(args.name, host=_host_of(args.name, hosts))
    if keys is None:
        session.send(args.text)
    else:
        session.press(keys)
    return 0


def _run(args, flags: list[str], hosts: Hosts, start_sid: str | None) -> int:
    cmd = args.cmd
    if cmd == "start":
        return _start(args, flags, hosts, start_sid)
    if cmd in ("restart", "resume"):
        return _worker(args, start_sid)
    if cmd in BATCH:
        return _batch(args, hosts)
    if cmd == "ls":
        return _ls(args, hosts)
    if cmd == "send":
        return _send(args, hosts)
    if cmd == "reply":
        text = Session.attach(args.name, host=_host_of(args.name, hosts)).reply
        if text:
            print(text)
    elif cmd == "read":
        print(_shown(args.name, hosts).screen(args.name, args.lines))
    elif cmd == "client-resume":
        return Session.client_resume(args.client, args.arguments)
    else:
        host = Session.host()
        if not isinstance(host, Displayable):
            raise NotDisplayable(f"session {args.window}: host {host.name} cannot display")
        host.tile(args.window, wait=not args.no_wait, if_panes_changed=args.if_panes_changed)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["hook"]:
        tui_hook.run(argv[1:], sys.stdin, roots=module_roots(agent_pm.session, agent_pm.utils))
        return 0
    start_sid = os.environ.get(START_SID) or None
    flags: list[str] = []
    if argv[:1] == ["start"] and "--" in argv:  # argparse < 3.13 drops a later -- from nargs
        at = argv.index("--")
        argv, flags = argv[:at], argv[at + 1 :]
    try:
        hosts, failure = _configure(), None
    except (ConfigError, OSError, ValueError) as error:
        hosts, failure = None, error
    args = _parser(hosts).parse_args(argv)
    if hosts is None:
        _say(f"{PROG}: config: {failure}")
        return 2
    try:
        return _run(args, flags, hosts, start_sid)
    except NotDisplayable as error:
        _say(str(error))
        return 1
    except ConfigError as error:
        _say(f"{PROG}: {error}")
        return 2
    except (SessionError, OSError) as error:
        _say(f"{PROG}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
