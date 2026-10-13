"""Bundled tmux anchors operations, independent of the standalone facade."""

from __future__ import annotations

import os
import runpy
import subprocess
from typing import TYPE_CHECKING

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux.commands import (
        _tmux,
        _tmux_ok,
    )
    from agent_pm.session.hosts.tmux.errors.types import (
        TuiError,
    )
    from agent_pm.session.hosts.tmux.model.types import (
        CLIENTS,
        GRID_PANES,
        GRID_ROW,
        NAME,
        OPENER,
        PANE,
        PANES,
        SESSION_ID,
        SESSIONS,
        Anchor,
    )
else:
    _load_file = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_file"]
    _commands = _load_file(
        "agent_pm.session.hosts.tmux.commands", os.path.join(DIRECTORY, "commands.py")
    )
    _tmux = _commands._tmux
    _tmux_ok = _commands._tmux_ok
    _errors_types = _load_file(
        "agent_pm.session.hosts.tmux.errors.types", os.path.join(DIRECTORY, "errors/types.py")
    )
    TuiError = _errors_types.TuiError
    _model_types = _load_file(
        "agent_pm.session.hosts.tmux.model.types", os.path.join(DIRECTORY, "model/types.py")
    )
    Anchor = _model_types.Anchor
    CLIENTS = _model_types.CLIENTS
    GRID_PANES = _model_types.GRID_PANES
    GRID_ROW = _model_types.GRID_ROW
    NAME = _model_types.NAME
    OPENER = _model_types.OPENER
    PANE = _model_types.PANE
    PANES = _model_types.PANES
    SESSIONS = _model_types.SESSIONS
    SESSION_ID = _model_types.SESSION_ID


def _sessions(proc) -> list[list[str]]:
    res = _tmux(["list-sessions", "-F", SESSIONS], proc)
    lines = res.stdout.split("\n") if res.returncode == 0 else []
    return [
        f
        for f in (line.split("\t") for line in lines)
        if len(f) == 5 and SESSION_ID.fullmatch(f[0])
    ]


def _window(
    pane: str, manager: str | None, proc
) -> tuple[str, str | None, dict[str, int], str] | None:
    """pane's window id, manager, pane areas and socket; None when tmux fails."""
    res = _tmux(["list-panes", "-t", pane, "-F", GRID_PANES], proc)
    rows = []
    if res.returncode:
        return None
    for line in res.stdout.splitlines():
        match = GRID_ROW.fullmatch(line)
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
    if not PANE.fullmatch(pane):
        raise TuiError.invalid_own_pane(pane)
    name = _tmux_ok(["display-message", "-p", "-t", pane, "#{session_name}"], proc).stdout.strip()
    if not NAME.fullmatch(name):
        raise TuiError.invalid_own_session(name)
    return name


def opener(*, proc=subprocess.run) -> str:
    """The caller's tmux session, else (iTerm.app) its full $ITERM_SESSION_ID; TuiError when neither
    matches OPENER.
    """
    return _opener(proc)


def _opener(proc) -> str:
    own = own_session(proc=proc)
    if own is not None:
        return own
    value = os.environ.get("ITERM_SESSION_ID", "")
    if not value or os.environ.get("TERM_PROGRAM") != "iTerm.app":
        raise TuiError.no_anchor()
    if ":" not in value or not OPENER.fullmatch(value):
        raise TuiError.invalid_iterm_session(value, OPENER.pattern)
    return value


def anchor(split_from: str | None = None, *, proc=subprocess.run) -> Anchor:
    """split_from's shown pane, else the caller's session's, else (iTerm.app only)
    $ITERM_SESSION_ID's; TuiError when none.
    """
    if split_from is not None:
        found = _shown(split_from, proc, own=False)
        if found is None:
            raise TuiError.unshown_session(split_from)
        return found
    own = own_session(proc=proc)
    return _iterm_pane("not in tmux") if own is None else _opener_pane(own, proc, own=True)


def _opener_pane(opener: str, proc, *, own: bool | None) -> Anchor:
    if ":" in opener:
        return Anchor(("id", [opener.partition(":")[2]]))
    if own is None:
        own = own_session(proc=proc) == opener
    return _shown(opener, proc, own=own) or _iterm_pane(f"no terminal shows tmux session {opener}")


def _shown(session: str, proc, *, own: bool) -> Anchor | None:
    """The pane the most recent client shows the session in: a nested client's host pane, $TMUX_PANE
    when own, else the client's; a control-mode client gets its tmux pane only.
    """
    clients = _clients(session, proc)
    if not clients:
        return None
    _, tty, pane, control, socket = clients[0]
    pane = os.environ["TMUX_PANE"] if own else pane
    if control == "1":
        return Anchor(None, pane, socket, session)
    hosts = {
        f[1]: f[2]
        for f in (
            line.split(" ", 2)
            for line in _tmux_ok(["list-panes", "-a", "-F", PANES], proc).stdout.splitlines()
        )
        if len(f) == 3 and f[0] != "1"
    }
    if PANE.fullmatch(hosts.get(tty, "")):
        return Anchor(None, hosts[tty], socket, session)
    return Anchor(("tty", [c[1] for c in clients if c[3] == "0"]), pane, socket, session)


def _clients(session: str, proc) -> list[list[str]]:
    out = _tmux_ok(["list-clients", "-t", f"={session}", "-F", CLIENTS], proc).stdout
    return sorted(
        (
            c
            for c in (line.split(" ", 4) for line in out.splitlines())
            if len(c) == 5
            and c[0].isdigit()
            and c[1]
            and PANE.fullmatch(c[2])
            and c[3] in ("0", "1")
            and c[4]
        ),
        key=lambda c: int(c[0]),
        reverse=True,
    )


def _iterm_pane(why: str) -> Anchor:
    unique = os.environ.get("ITERM_SESSION_ID", "").partition(":")[2]
    if not unique or not (os.environ.get("TMUX") or os.environ.get("TERM_PROGRAM") == "iTerm.app"):
        raise TuiError.no_anchor(why)
    return Anchor(("id", [unique]))
