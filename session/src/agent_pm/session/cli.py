"""agent-pm-session, the session layer's command (FR-31, FR-42; bin/agent-pm-session runs it).

start, restart and resume a worker (engine/worker.py) and print its Record; reply, send (text, or
keys in KeyPress notation) and read a session; status, show, hide, close and attach-command for
session names, or Records one per stdin line; ls; tile; hook, a tui client's hook call (tui_hook.run).
Every subcommand but hook reads the session config once: read_config, Host.parse_hosts,
Session.configure. AGENT_PM_SID, read once here, is a start's or resume's session id. Inside an
agent session agent-pm started (AGENT_PM_AGENT_SESSION), start, restart, resume and client-resume
refuse.

stdout holds only machine output: Records, Status lines, ls rows, read and reply text, attach
commands. Exit 0 done, 2 a usage or config error, 1 a failed key or other failure. A batch answers
its keys in order; a failed key gets one stderr line and no stdout line, and the exit is 1.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
import uuid
from collections.abc import Iterator
from dataclasses import replace

import agent_pm.session.engine.types
import agent_pm.utils
from agent_pm.session.engine.base import Session
from agent_pm.session.engine.config import read_config
from agent_pm.session.engine.types import (
    COMMAND,
    NO_DISPLAY,
    SESSION_NAME,
    SESSION_NAME_RULE,
    ConfigError,
    Hosts,
    KeyPress,
    NotShowable,
    SessionError,
    SessionInfo,
    SessionRow,
)
from agent_pm.session.engine.worker import Worker
from agent_pm.session.hosts.base import Host, Showable, check_sid
from agent_pm.session.runtimes import tui_hook
from agent_pm.session.runtimes.tui import TuiRuntime
from agent_pm.utils import (
    EVENTS_ENV,
    SID_ENV,
    Record,
    Status,
    StatusState,
    answer_keys,
    module_roots,
    nested_start,
)

PROG = COMMAND
HEADER = "name\tstate\tclient\tsid\tdisplay"


class UsageError(ConfigError):
    """A malformed request."""


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _name(value: str) -> str:
    if not SESSION_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(f"want {SESSION_NAME_RULE}, not {value!r:.80}")
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
        "and any session for status, show, hide, close, attach-command, ls, send, read and tile.",
        epilog=_display_help(hosts),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = sub.add_parser(
        "start",
        help="start worker NAME; print its Record",
        description="Start worker NAME, a plain tui session of CLIENT; everything after -- is "
        "its client's flags, verbatim. Its session id: --resume SID (resumes that "
        f"conversation), else ${SID_ENV}, else a new one; the two must agree. Prints its "
        "Record once it has run 5 s without exiting (up to 15 s while its launch input is "
        "pending).",
    )
    p.add_argument("name", type=_name)
    p.add_argument("--client", required=True, help="its client, a [clients.<name>]")
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
    p.add_argument(
        "--display",
        metavar="D",
        help=f"where its Host shows it; {NO_DISPLAY}: shown nowhere until show (see below)",
    )
    for cmd, text in (
        ("restart", "restart a worker's session in place; print its Record"),
        ("resume", "print a live worker's Record, else resume it; print its Record"),
        ("reply", "print a tui session's latest reply"),
    ):
        p = sub.add_parser(cmd, help=text, description=text)
        p.add_argument("name", type=_name)
        if cmd == "reply":
            p.add_argument("--sid", help="require this session id")
    for cmd, text in (
        ("status", "one Status JSON line per session"),
        ("content", "print one session's content"),
        ("show", "show each session where its display says"),
        ("hide", "close what show opened for each session; it runs on"),
        ("close", "close each session; absent is fine"),
        ("attach-command", "print each session's attach command"),
    ):
        p = sub.add_parser(cmd, help=text, description=f"{text}: names, else Records on stdin.")
        p.add_argument("names", nargs="*", type=_name, metavar="NAME")
        if cmd == "hide":
            p.add_argument(
                "--if-closed",
                action="store_true",
                help="only once what show opened was closed by hand (hooks)",
            )
    p = sub.add_parser(
        "ls",
        help="list sessions",
        description="List sessions (see docs/sessions.md › agent-pm-session) or, with --records, the "
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
        help="with --here --records: also those they opened, transitively (close-all)",
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
    p.add_argument("--sid", help="require this session id")
    p.add_argument("--key", nargs="+", metavar="K", help="keys, all checked before any is sent")
    p = sub.add_parser("read", help="print a session's screen")
    p.add_argument("name", type=_name)
    p.add_argument("--sid", help="require this session id")
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


def _shown(name: str, hosts: Hosts) -> Showable:
    host = Session.host(_host_of(name, hosts))
    if not isinstance(host, Showable):
        raise NotShowable(f"session {name}: host {host.name} cannot show")
    return host


def _resolve(key: str | Record) -> tuple[str, tuple[str, Record | None]]:
    name, record = (key, None) if isinstance(key, str) else (key.name, key)
    sessions = _sessions(name, record)
    if not sessions:
        raise UsageError("names no session")
    for session in sessions:
        if not SESSION_NAME.fullmatch(session):
            raise UsageError(f"{name}: session {session!r:.80}: want {SESSION_NAME_RULE}")
    return name, (name, record)


def _sessions(name: str, record: Record | None) -> tuple[str, ...]:
    return (name,) if record is None else record.sessions


def _state(name: str, hosts: Hosts, sid: str | None = None) -> Status:
    """Session `name`'s own Status: gone once its host no longer lists it."""
    host = Session.host(_host_of(name, hosts))
    process = host.attach(name) if name in host.list() else None
    try:
        check_sid(name, process, sid)
    except SessionError:
        process = None
    if process is None:
        return Status(
            name=name, alive=False, state=StatusState.GONE, outcome=None, attach=None, hidden=False
        )
    found = process.status()
    return replace(found, cwd=_cwd(name)) if found.alive else found


def _cwd(name: str) -> str | None:
    """Tui session `name`'s cwd as its start recorded it; None for any other session."""
    try:
        info = SessionInfo.from_fields(TuiRuntime.snapshot(name))
    except SessionError:
        return None
    return None if info is None else info.cwd


def _status(name: str, record: Record | None, hosts: Hosts) -> str:
    found = [
        _state(session, hosts, None if record is None else record.sid)
        for session in _sessions(name, record)
    ]
    shown = next((each for each in found if each.alive), found[0])
    if record is not None and record.sid is not None:
        shown = next((each for each in found if each.state == StatusState.GONE), shown)
    return replace(shown, name=name, hidden=any(each.hidden for each in found)).to_line()


def _carried(
    name: str, record: Record | None, hosts: Hosts, *, stale_ok: bool = False
) -> tuple[str, ...]:
    sessions = _sessions(name, record)
    if record is None:
        return sessions
    for session in sessions:
        host = Session.host(_host_of(session, hosts))
        process = host.attach(session) if session in host.list() else None
        try:
            check_sid(session, process, record.sid)
        except SessionError:
            if stale_ok:
                return ()
            raise
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


def _hide(name: str, record: Record | None, hosts: Hosts, *, if_closed: bool = False) -> None:
    for session in _carried(name, record, hosts):
        _shown(session, hosts).hide(session, if_closed=if_closed)


def _attach_command(name: str, record: Record | None, hosts: Hosts) -> str:
    session = _carried(name, record, hosts)[0]
    return _shown(session, hosts).attach_command(session)


def _close(name: str, record: Record | None, hosts: Hosts) -> None:
    for session in _carried(name, record, hosts, stale_ok=True):
        try:
            Session.retire(
                session, sid=None if record is None else record.sid, host=_host_of(session, hosts)
            )
        except SessionError:
            if record is None or _carried(name, record, hosts, stale_ok=True):
                raise
            return


def _content(name: str, record: Record | None, hosts: Hosts) -> str:
    session = _carried(name, record, hosts)[0]
    return Session.attach(
        session, sid=None if record is None else record.sid, host=_host_of(session, hosts)
    ).content()


BATCH = {
    "status": _status,
    "content": _content,
    "show": _show,
    "hide": _hide,
    "close": _close,
    "attach-command": _attach_command,
}


def _answers(args, hosts: Hosts) -> int:
    act = BATCH[args.cmd]
    if args.cmd == "hide":
        act = functools.partial(_hide, if_closed=args.if_closed)
    if not args.names and (sys.stdin is None or sys.stdin.isatty()):
        raise UsageError(f"{args.cmd}: give session names or Records on stdin")
    try:
        return answer_keys(
            args.names,
            sys.stdin,
            _resolve,
            lambda key: act(*key, hosts),
            single=args.cmd == "content",
            usage=(UsageError,),
            failures=(ConfigError, SessionError, OSError),
            prog=PROG,
            out=sys.stdout,
            err=sys.stderr,
        )
    except UnicodeDecodeError:
        raise UsageError(f"{args.cmd}: stdin: not UTF-8") from None


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
        raise UsageError(f"{SID_ENV} {start_sid} differs from --resume {args.resume}")
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if not os.path.isdir(cwd):
        raise UsageError(f"--cwd {cwd}: not a directory")
    events = os.environ.get(EVENTS_ENV) or None
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
        record = worker.start(
            prompt=args.prompt,
            resume=args.resume is not None,
            hidden=args.display == NO_DISPLAY,
        )
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
    session = Session.attach(args.name, sid=args.sid, host=_host_of(args.name, hosts))
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
        return _answers(args, hosts)
    if cmd == "ls":
        return _ls(args, hosts)
    if cmd == "send":
        return _send(args, hosts)
    if cmd == "reply":
        text = Session.attach(args.name, sid=args.sid, host=_host_of(args.name, hosts)).reply
        if text:
            print(text)
    elif cmd == "read":
        host = Session.host(_host_of(args.name, hosts))
        check_sid(args.name, host.attach(args.name), args.sid)
        print(_shown(args.name, hosts).screen(args.name, args.lines))
    elif cmd == "client-resume":
        return Session.client_resume(args.client, args.arguments)
    else:
        host = Session.host()
        if not isinstance(host, Showable):
            raise NotShowable(f"session {args.window}: host {host.name} cannot show")
        host.tile(args.window, wait=not args.no_wait, if_panes_changed=args.if_panes_changed)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["hook"]:
        tui_hook.run(argv[1:], sys.stdin, roots=module_roots(agent_pm.session, agent_pm.utils))
        return 0
    start_sid = os.environ.get(SID_ENV) or None
    flags: list[str] = []
    if argv[:1] == ["start"] and "--" in argv:  # argparse < 3.13 drops a later -- from nargs
        at = argv.index("--")
        argv, flags = argv[:at], argv[at + 1 :]
    try:
        hosts, failure = _configure(), None
    except (ConfigError, OSError, ValueError) as error:
        hosts, failure = None, error
    args = _parser(hosts).parse_args(argv)
    starts = args.cmd in {"start", "restart", "resume", "client-resume"}
    if starts and (refused := nested_start()):
        _say(f"{PROG}: {refused}")
        return 2
    if hosts is None:
        _say(f"{PROG}: config: {failure}")
        return 2
    try:
        return _run(args, flags, hosts, start_sid)
    except NotShowable as error:
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
