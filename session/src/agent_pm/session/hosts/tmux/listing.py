"""Tmux session listing and display diagnostics."""

import subprocess

from agent_pm.session.hosts.tmux.transport import (
    ITERM_ID,
    NAME,
    OPENER,
    PANE,
    absent,
    display_error,
    read,
    run,
)
from agent_pm.session.hosts.tmux.types import TuiError

LISTING = "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@client}\t#{@state}\t#{@pane}\t#{@opener}\t#{@display}"


SHOWN = "#{client_activity}\t#{client_tty}\t#{session_name}"


HOSTS = "#{pane_dead}\t#{pane_tty}\t#{session_name}"


LAST_LINES = 2000


def listing(match=None, *, proc=subprocess.run) -> list[dict] | None:
    """Sessions named NAME that match (compiled; None: all) fullmatches, by name: tags, pane state,
    `shown` (no client: None; else the newest client's host session, "" if none or not NAME), a dead
    pane's last LAST_LINES lines; a bad or empty field None, a row of the wrong width dropped. []
    when no server runs, None when tmux fails.
    """
    found = _table(["list-sessions", "-F", LISTING], 8, proc)
    if found is None:
        return None
    rows = sorted(
        f for f in found if NAME.fullmatch(f[0]) and (match is None or match.fullmatch(f[0]))
    )
    if not rows:
        return []
    clients = _table(["list-clients", "-F", SHOWN], 3, proc)
    if clients is None:
        return None
    recent = {}
    for _, tty, session in sorted(
        (c for c in clients if c[0].isdigit() and c[1]), key=lambda c: int(c[0])
    ):
        recent[session] = tty
    hosts = {}
    if any(f[0] in recent for f in rows):
        panes = _table(["list-panes", "-a", "-F", HOSTS], 3, proc)
        if panes is None:
            return None
        hosts = {tty: session for dead, tty, session in panes if dead != "1"}
    out = []
    for name, dead, sid, client, state, pane, parent, display in rows:
        tty = recent.get(name)
        host = "" if tty is None else hosts.get(tty, "")
        out.append(
            {
                "name": name,
                "sid": _printable(sid),
                "client": _printable(client),
                "state": _printable(state),
                "pane": pane if PANE.fullmatch(pane) or ITERM_ID.fullmatch(pane) else None,
                "opener": parent if OPENER.fullmatch(parent) else None,
                "display": display if display and not display_error(display) else None,
                "dead": {"1": True, "0": False}.get(dead),
                "shown": None if name not in recent else host if NAME.fullmatch(host) else "",
                "last_lines": _last_lines(name, proc) if dead == "1" else None,
            }
        )
    return out


def _table(args: list[str], width: int, proc) -> list[list[str]] | None:
    try:
        # -u: else, without a UTF-8 locale or $TMUX, tmux prints tabs as _
        res = run(["-u", *args], proc)
    except TuiError:
        return None
    if res.returncode:
        return [] if absent(res) else None
    return [f for f in (line.split("\t") for line in res.stdout.split("\n")) if len(f) == width]


def _printable(value: str) -> str | None:
    return value if value and value.isprintable() else None


def _last_lines(name: str, proc) -> str | None:
    try:
        return read(name, LAST_LINES, proc=proc) or None
    except TuiError:
        return None
