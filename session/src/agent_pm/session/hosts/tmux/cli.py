"""Tmux host command-line interface."""

from __future__ import annotations

import argparse
import os
import re
import sys

from agent_pm.session.engine.types import Launch
from agent_pm.session.hosts.tmux.grid import (
    PER_COLUMN,
    RETILE,
    tile,
)
from agent_pm.session.hosts.tmux.host import Tmux
from agent_pm.session.hosts.tmux.placement import (
    show,
)
from agent_pm.session.hosts.tmux.transport import (
    session_name,
)
from agent_pm.session.hosts.tmux.types import (
    Retile,
    Split,
    TuiError,
)


def main(argv: list[str] | None = None, *, operations=None) -> int:
    tmux = Tmux()
    actions = {
        "start": tmux.start,
        "send": tmux.send,
        "read": tmux.read,
        "tile": tile,
        "status": tmux.status,
        "show": show,
        **(operations or {}),
    }
    ap = argparse.ArgumentParser(
        prog="entry.py",
        description="Host a command, e.g. an agent's TUI, in a "
        "detached tmux session: start it, "
        "type into it, read it, show it. The show: --show T; else, opened from tmux, a grid "
        "in the manager's window (it stays left, the panes it opens fill columns of "
        "--per-column, top down, then right); else an iTerm2 split, else a tmux split. T may "
        "use {{session}}; '' prints only the attach command. "
        "$TUI_ATTACH_PREFIX (e.g. 'docker exec -it C') prefixes printed attach commands.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    start_p = sub.add_parser(
        "start",
        help="run a command in a new detached session (show shows it)",
        description="session -- command [args...]: everything after the first -- is the "
        "command, passed through verbatim.",
    )
    start_p.add_argument(
        "--events",
        metavar="FILE",
        help="append `HH:MM:SS <session> done|blocked|dead` lines to FILE (created 0600)",
    )
    start_p.add_argument("session", type=_session_arg)
    p = sub.add_parser("send", help="type text into the session, then Enter")
    p.add_argument("session", type=_session_arg)
    p.add_argument("text")
    p = sub.add_parser("read", help="print the session's pane text")
    p.add_argument("session", type=_session_arg)
    p.add_argument("--lines", type=_lines_arg, metavar="N", help="the last N lines, with history")
    p = sub.add_parser("show", help="show a running session")
    _show_options(p)
    p.add_argument("session", type=_session_arg)
    p = sub.add_parser("tile", help="lay out a grid window's panes (its hooks run this)")
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="return at once while another tile lays the window out (the hooks pass it)",
    )
    p.add_argument(
        "--if-panes-changed",
        action="store_true",
        help=(
            "lay out only when the window's panes are not those of its last tile (open-close's hooks "
            "pass it: a drag or resize keeps the layout)"
        ),
    )
    p.add_argument("window", help="its id, @N")
    argv = sys.argv[1:] if argv is None else list(argv)
    command = None
    if argv[:1] == ["start"] and "--" in argv:  # argparse < 3.13 drops a later -- from a nargs list
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1 :]
    elif argv[:1] == ["start"] and not {"-h", "--help"} & set(argv):
        start_p.error("a command must follow --")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            if not command:
                start_p.error("a command must follow --")
            actions["start"](
                a.session,
                Launch(tuple(command), dict(os.environ), cwd=os.getcwd()),
                events=a.events,
            )
        elif a.cmd == "send":
            actions["send"](a.session, a.text)
        elif a.cmd == "read":
            print(actions["read"](a.session, a.lines))
        elif a.cmd == "tile":
            actions["tile"](a.window, wait=not a.no_wait, if_panes_changed=a.if_panes_changed)
        else:
            if actions["status"](a.session) is None:
                raise TuiError(f"no session {a.session}")
            return (
                1
                if actions["show"](
                    a.session,
                    a.show,
                    split=a.split,
                    split_from=a.split_from,
                    per_column=a.per_column,
                    retile=a.grid_retile,
                )
                else 0
            )
    except TuiError as e:
        print(f"tui: {e}", file=sys.stderr)
        return 1
    return 0


def _show_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--show", metavar="T", help="the show command, instead of the split")
    p.add_argument(
        "--split",
        choices=[s.value for s in Split],
        help="the split's side outside the grid (default: below the newest pane "
        "you opened that still shows, else right of yours; with "
        "--split-from, right)",
    )
    p.add_argument(
        "--split-from",
        metavar="S",
        type=_session_arg,
        help="split the pane showing tmux session S (default: yours), outside the grid",
    )
    p.add_argument(
        "--per-column",
        metavar="N",
        type=_per_column_arg,
        default=PER_COLUMN,
        help=f"panes per grid column, 1 to 9999 (default {PER_COLUMN})",
    )
    p.add_argument(
        "--grid-retile",
        metavar="M",
        choices=[r.value for r in Retile],
        default=RETILE,
        help=(
            f"when the grid re-tiles by itself: {RETILE} (default) when a pane opens or closes; all also "
            "on a drag or a window resize; off never (tile @N)"
        ),
    )


def _session_arg(value: str) -> str:
    try:
        return session_name(value)
    except TuiError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _per_column_arg(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,4}", value) or int(value) < 1:
        raise argparse.ArgumentTypeError(f"want an int from 1 to 9999, not {value!r}")
    return int(value)


def _lines_arg(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"want a positive int, not {value!r}")
    return int(value)
