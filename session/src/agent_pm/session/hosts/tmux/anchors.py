"""Bundled tmux anchors operations, independent of the standalone facade."""

from __future__ import annotations

import os
import subprocess
from os.path import dirname, join
from runpy import run_path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux import commands as _commands
    from agent_pm.session.hosts.tmux.errors import types as _errors
    from agent_pm.session.hosts.tmux.model import types as _model
else:
    load_controls = run_path(join(dirname(__file__), "loading/types.py"))["load_controls"]
    _commands, _errors, _model = load_controls(dirname(__file__))


def list_sessions(proc) -> list[list[str]]:
    res = _commands.call_tmux(["list-sessions", "-F", _model.SESSIONS], proc)
    lines = res.stdout.split("\n") if res.returncode == 0 else []
    return [
        f
        for f in (line.split("\t") for line in lines)
        if len(f) == 5 and _model.SESSION_ID.fullmatch(f[0])
    ]


def pane_window(
    pane: str, manager: str | None, proc
) -> tuple[str, str | None, dict[str, int], str] | None:
    """pane's window id, manager, pane areas and socket; None when tmux fails."""
    res = _commands.call_tmux(["list-panes", "-t", pane, "-F", _model.GRID_PANES], proc)
    rows = []
    if res.returncode:
        return None
    for line in res.stdout.splitlines():
        match = _model.GRID_ROW.fullmatch(line)
        if match is None:
            return None
        rows.append(match)
    if not rows:
        return None
    areas = {m[3]: int(m[4]) * int(m[5]) for m in rows}
    window, stored = rows[0].group(1, 2)
    return window, stored if stored in areas else manager, areas, rows[0][6]


def own_session(*, proc=subprocess.run) -> str | None:
    """$TMUX_PANE's session; None outside tmux."""
    if not os.environ.get("TMUX"):
        return None
    pane = os.environ.get("TMUX_PANE", "")
    if not _model.PANE.fullmatch(pane):
        raise _errors.TuiError.invalid_own_pane(pane)
    name = _commands.checked_tmux(
        ["display-message", "-p", "-t", pane, "#{session_name}"], proc
    ).stdout.strip()
    if not _model.NAME.fullmatch(name):
        raise _errors.TuiError.invalid_own_session(name)
    return name


def opener(*, proc=subprocess.run) -> str:
    """The caller's tmux session, else (iTerm.app) its full $ITERM_SESSION_ID; TuiError when neither
    matches OPENER.
    """
    return find_opener(proc)


def find_opener(proc) -> str:
    own = own_session(proc=proc)
    if own is not None:
        return own
    value = os.environ.get("ITERM_SESSION_ID", "")
    if not value or os.environ.get("TERM_PROGRAM") != "iTerm.app":
        raise _errors.TuiError.no_anchor()
    if ":" not in value or not _model.OPENER.fullmatch(value):
        raise _errors.TuiError.invalid_iterm_session(value, _model.OPENER.pattern)
    return value


def anchor(split_from: str | None = None, *, proc=subprocess.run) -> _model.Anchor:
    """split_from's shown pane, else the caller's session's, else (iTerm.app only)
    $ITERM_SESSION_ID's; TuiError when none.
    """
    if split_from is not None:
        found = shown_pane(split_from, proc, own=False)
        if found is None:
            raise _errors.TuiError.unshown_session(split_from)
        return found
    own = own_session(proc=proc)
    return iterm_pane("not in tmux") if own is None else opener_pane(own, proc, own=True)


def opener_pane(opener: str, proc, *, own: bool | None) -> _model.Anchor:
    if ":" in opener:
        return _model.Anchor(("id", [opener.partition(":")[2]]))
    if own is None:
        own = own_session(proc=proc) == opener
    return shown_pane(opener, proc, own=own) or iterm_pane(
        f"no terminal shows tmux session {opener}"
    )


def shown_pane(session: str, proc, *, own: bool) -> _model.Anchor | None:
    """The pane the most recent client shows the session in: a nested client's host pane, $TMUX_PANE
    when own, else the client's; a control-mode client gets its tmux pane only.
    """
    clients = list_clients(session, proc)
    if not clients:
        return None
    _, tty, pane, control, socket = clients[0]
    pane = os.environ["TMUX_PANE"] if own else pane
    if control == "1":
        return _model.Anchor(None, pane, socket, session)
    hosts = {
        f[1]: f[2]
        for f in (
            line.split(" ", 2)
            for line in _commands.checked_tmux(
                ["list-panes", "-a", "-F", _model.PANES], proc
            ).stdout.splitlines()
        )
        if len(f) == 3 and f[0] != "1"
    }
    if _model.PANE.fullmatch(hosts.get(tty, "")):
        return _model.Anchor(None, hosts[tty], socket, session)
    return _model.Anchor(("tty", [c[1] for c in clients if c[3] == "0"]), pane, socket, session)


def list_clients(session: str, proc) -> list[list[str]]:
    out = _commands.checked_tmux(
        ["list-clients", "-t", f"={session}", "-F", _model.CLIENTS], proc
    ).stdout
    return sorted(
        (
            c
            for c in (line.split(" ", 4) for line in out.splitlines())
            if len(c) == 5
            and c[0].isdigit()
            and c[1]
            and _model.PANE.fullmatch(c[2])
            and c[3] in ("0", "1")
            and c[4]
        ),
        key=lambda c: int(c[0]),
        reverse=True,
    )


def iterm_pane(why: str) -> _model.Anchor:
    unique = os.environ.get("ITERM_SESSION_ID", "").partition(":")[2]
    if not unique or not (os.environ.get("TMUX") or os.environ.get("TERM_PROGRAM") == "iTerm.app"):
        raise _errors.TuiError.no_anchor(why)
    return _model.Anchor(("id", [unique]))
