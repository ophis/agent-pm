"""Tmux display placement and caller context."""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys

from agent_pm.session.hosts.tmux.grid import (
    ENTRY,
    GRID_BUSY,
    GRID_PANES,
    GRID_ROW,
    PER_COLUMN,
    RETILE,
    configure,
    validate_per_column,
    validate_retile,
)
from agent_pm.session.hosts.tmux.iterm import (
    SHOW_TIMEOUT,
    split_pane,
)
from agent_pm.session.hosts.tmux.transport import (
    CLIENTS,
    ITERM_ID,
    NAME,
    OPENER,
    PANE,
    PANES,
    SESSION_ID,
    SESSIONS,
    attach_command,
    checked,
    error,
    run,
    session_name,
)
from agent_pm.session.hosts.tmux.types import (
    Anchor,
    ItermAnchor,
    Retile,
    Split,
    TmuxAnchor,
    TuiError,
)

PLACEHOLDERS = {"session"}


SLOT = re.compile(r"\{\{([^{}]*)\}\}")


NO_PANE = "no anchor pane: {}, no iTerm2 pane ($ITERM_SESSION_ID)"


def show(
    session: str,
    template: str | None = None,
    *,
    split: str | None = None,
    split_from: str | None = None,
    opener: str | None = None,
    per_column: int | None = None,
    retile: str | None = None,
    script: str = ENTRY,
    proc=subprocess.run,
    tile_window=None,
) -> str | None:
    """Prints how to attach, then runs template, else open_pane; "" runs nothing. A failure is
    returned, never raised, but open_pane's TuiError for a bad argument.
    """
    print(f"tui: session {session}: {attach_command(session)}", file=sys.stderr)
    if template is None:
        why = open_pane(
            session,
            split=split,
            split_from=split_from,
            opener=opener,
            per_column=per_column,
            retile=retile,
            script=script,
            proc=proc,
            tile_window=tile_window,
        )
    else:
        why = _run(session, template, proc) if template else None
    if why:
        print(f"tui: show: {why}", file=sys.stderr)
    return why


def open_pane(
    session: str,
    *,
    split: str | None = None,
    split_from: str | None = None,
    opener: str | None = None,
    per_column: int | None = None,
    retile: str | None = None,
    script: str = ENTRY,
    proc=subprocess.run,
    tile_window=None,
) -> str | None:
    """Opens a pane on the session, recording @opener and @pane. Grid mode (_grid): splits the pane
    _grid picks and tiles the window per retile and per_column, ignoring split and split_from. Else
    below the newest open @pane of the opener's other sessions, else right of the opener's pane; or
    beside split_from's pane on side split. None or the failure.
    """
    _layout(split, split_from, opener)
    validate_per_column(per_column)
    validate_retile(retile)
    tmux = shutil.which("tmux")
    if tmux is None:
        return "tmux not found"
    tmux = os.path.abspath(tmux)
    own = grid = new = None
    try:
        if opener is None:
            try:
                opener, own = _opener(proc), True
            except TuiError:
                if split_from is None:
                    raise
        in_tmux = opener is not None and ":" not in opener
        sessions = _sessions(proc) if in_tmux or (split is None and split_from is None) else []
        grid = _grid(opener, sessions, proc, own=own) if opener is not None and in_tmux else None
        if grid is not None:
            window, manager, at, side = grid
            if split is not None or split_from is not None:
                print("tui: show: grid: --split/--split-from ignored", file=sys.stderr)
            # muted: an old mode's hook tile could land after a later drag; _grid_up unmutes
            run(["set-option", "-w", "-t", window, GRID_BUSY, window], proc)
            try:
                new, why = _split(session, side, at, tmux, proc)
            finally:
                if new is None:
                    with contextlib.suppress(TuiError):
                        run(["set-option", "-u", "-w", "-t", window, GRID_BUSY], proc)
        elif split_from is not None:
            new, why = _split(session, split or "right", anchor(split_from, proc=proc), tmux, proc)
        elif split is not None:
            if opener is None:
                raise TuiError(NO_PANE.format("not in tmux"))
            new, why = _split(session, split, _opener_pane(opener, proc, own=own), tmux, proc)
        else:
            if opener is None:
                raise TuiError(NO_PANE.format("not in tmux"))
            new, why = _stack(session, opener, sessions, tmux, proc) or _split(
                session, "right", _opener_pane(opener, proc, own=own), tmux, proc
            )
    except TuiError as e:
        return f"{e}; watch it with {attach_command(session)}"
    if new is None:
        return why
    _record(session, opener, new, proc)
    if grid is None:
        return None
    return configure(
        window,
        manager,
        script=script,
        proc=proc,
        tile_window=tile_window,
        per_column=PER_COLUMN if per_column is None else per_column,
        retile=RETILE if retile is None else Retile(retile),
    )


def _sessions(proc) -> list[list[str]]:
    res = run(["list-sessions", "-F", SESSIONS], proc)
    lines = res.stdout.split("\n") if res.returncode == 0 else []
    return [
        f
        for f in (line.split("\t") for line in lines)
        if len(f) == 5 and SESSION_ID.fullmatch(f[0])
    ]


def _grid(
    opener: str, sessions: list[list[str]], proc, *, own: bool | None
) -> tuple[str, str, Anchor, str] | None:
    """(grid window, manager, pane to split, side) when the opener is in a grid or a root a client
    shows; None: iTerm2 mode. It splits the largest pane but the manager below, else the manager
    right.
    """
    row = next((f for f in sessions if f[1] == opener), None)
    if row is None:
        return None
    try:
        found = _window(row[3], None, proc) if PANE.fullmatch(row[3]) else None
        if found is None:
            clients = [] if OPENER.fullmatch(row[2]) else _clients(opener, proc)
            if not clients:
                return None
            if own is None:
                own = own_session(proc=proc) == opener
            pane = os.environ["TMUX_PANE"] if own else clients[0][2]
            found = _window(pane, pane, proc)
    except TuiError:
        return None
    if found is None:
        return None
    window, manager, areas, socket = found
    workers = [p for p in areas if p != manager]
    if not workers:
        return window, manager, TmuxAnchor(manager, socket), "right"
    return (
        window,
        manager,
        TmuxAnchor(max(workers, key=lambda p: (areas[p], int(p[1:]))), socket),
        "below",
    )


def _window(pane: str, manager: str | None, proc) -> tuple[str, str, dict[str, int], str] | None:
    """pane's window id, manager, pane areas and socket; None when tmux fails."""
    res = run(["list-panes", "-t", pane, "-F", GRID_PANES], proc)
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
    manager = stored if stored in areas else manager
    return None if manager is None else (window, manager, areas, rows[0][6])


def _stack(
    session: str, opener: str, sessions: list[list[str]], tmux: str, proc
) -> tuple[str | None, str | None] | None:
    """None when no @pane of the opener's other sessions is open."""
    rows = [
        (int(f[0][1:]), f[3], f[4])
        for f in sessions
        if f[1] != session and f[2] == opener and (PANE.fullmatch(f[3]) or ITERM_ID.fullmatch(f[3]))
    ]
    live = None
    for is_tmux, group in itertools.groupby(
        sorted(rows, reverse=True), key=lambda r: bool(PANE.fullmatch(r[1]))
    ):
        group = list(group)
        if not is_tmux:  # open iff the AppleScript finds it: one try covers them, newest first
            with contextlib.suppress(TuiError):
                return _split(
                    session, "below", ItermAnchor(("id", [r[1] for r in group])), tmux, proc
                )
            continue
        if live is None:
            res = run(["list-panes", "-a", "-F", "#{pane_id}"], proc)
            live = set(res.stdout.split("\n")) if res.returncode == 0 else set()
        for _, pane, socket in group:
            if pane in live:
                return _split(session, "below", TmuxAnchor(pane, socket), tmux, proc)
    return None


def _split(session: str, side: str, a: Anchor, tmux: str, proc) -> tuple[str | None, str | None]:
    """(new pane, None) or (None, tmux's failure), in iTerm2 when one shows a's pane; TuiError with
    no pane.
    """
    if isinstance(a, ItermAnchor):
        try:
            return split_pane(session, side, tmux, a.iterm, proc), None
        except TuiError:
            if a.tmux is None:
                raise
            a = a.tmux
    command = [
        "env",
        "-u",
        "TMUX",
        tmux,
        "-S",
        a.socket,
        "attach",
        "-t",
        f"={session}",
    ]  # argv: as a sh -c string it died at once (tmux 3.7)
    for arg in command:
        if "#" in arg or arg.endswith(";"):
            raise TuiError(f"tmux would misread {arg!r}")
    try:
        res = run(
            [
                "split-window",
                "-d",
                "-h" if side == "right" else "-v",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                a.pane,
                *command,
            ],
            proc,
        )
    except TuiError as e:
        return None, str(e)
    return (None, f"tmux: {error(res)}") if res.returncode else (res.stdout.strip(), None)


def _record(session: str, opener: str | None, pane: str, proc) -> None:
    """For the next placement; a failure is only printed."""
    if not (PANE.fullmatch(pane) or ITERM_ID.fullmatch(pane)):
        print(f"tui: show: @pane: not a pane id: {pane!r}", file=sys.stderr)
        return
    for key, value in (("@opener", opener), ("@pane", pane)):
        if value is None:
            continue
        try:
            checked(["set-option", "-t", f"={session}:", key, value], proc)
        except TuiError as e:
            print(f"tui: show: {key}: {e}", file=sys.stderr)
            return


def _layout(split: str | None, split_from: str | None, opener: str | None = None) -> None:
    if split is not None and split not in tuple(Split):
        raise TuiError(f"split must be one of {', '.join(Split)}, not {split!r}")
    if split_from is not None:
        session_name(split_from)
    if opener is not None and not OPENER.fullmatch(opener):
        raise TuiError(
            f"invalid opener {opener!r}: want a tmux session name or $ITERM_SESSION_ID's value"
        )


def _run(session: str, template: str, proc) -> str | None:
    unknown = [m.group(0) for m in SLOT.finditer(template) if m.group(1) not in PLACEHOLDERS]
    if unknown:
        return f"unknown placeholder {unknown[0]}"
    try:
        res = proc(
            ["/bin/sh", "-c", SLOT.sub(lambda _match: shlex.quote(session), template)],
            stdin=subprocess.DEVNULL,
            timeout=SHOW_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {SHOW_TIMEOUT} s"
    except OSError as e:
        return str(e)
    return f"exit status {res.returncode}" if res.returncode else None


def own_session(*, proc=subprocess.run) -> str | None:
    """$TMUX_PANE's session; None outside tmux."""
    if not os.environ.get("TMUX"):
        return None
    pane = os.environ.get("TMUX_PANE", "")
    if not PANE.fullmatch(pane):
        raise TuiError(f"bad $TMUX_PANE {pane!r}")
    name = checked(["display-message", "-p", "-t", pane, "#{session_name}"], proc).stdout.strip()
    if not NAME.fullmatch(name):
        raise TuiError(f"own tmux session {name!r}: want [A-Za-z0-9_-]+")
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
        raise TuiError(NO_PANE.format("not in tmux"))
    if ":" not in value or not OPENER.fullmatch(value):
        raise TuiError(f"bad $ITERM_SESSION_ID {value!r}: want {OPENER.pattern}")
    return value


def anchor(split_from: str | None = None, *, proc=subprocess.run) -> Anchor:
    """split_from's shown pane, else the caller's session's, else (iTerm.app only)
    $ITERM_SESSION_ID's; TuiError when none.
    """
    if split_from is not None:
        found = _shown(split_from, proc, own=False)
        if found is None:
            raise TuiError(f"no terminal shows tmux session {split_from}")
        return found
    own = own_session(proc=proc)
    return _iterm_pane("not in tmux") if own is None else _opener_pane(own, proc, own=True)


def _opener_pane(opener: str, proc, *, own: bool | None) -> Anchor:
    if ":" in opener:
        return ItermAnchor(("id", [opener.partition(":")[2]]))
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
        return TmuxAnchor(pane, socket, session)
    hosts = {
        f[1]: f[2]
        for f in (
            line.split(" ", 2)
            for line in checked(["list-panes", "-a", "-F", PANES], proc).stdout.splitlines()
        )
        if len(f) == 3 and f[0] != "1"
    }
    if PANE.fullmatch(hosts.get(tty, "")):
        return TmuxAnchor(hosts[tty], socket, session)
    return ItermAnchor(
        ("tty", [c[1] for c in clients if c[3] == "0"]), TmuxAnchor(pane, socket, session)
    )


def _clients(session: str, proc) -> list[list[str]]:
    out = checked(["list-clients", "-t", f"={session}", "-F", CLIENTS], proc).stdout
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
        raise TuiError(NO_PANE.format(why))
    return ItermAnchor(("id", [unique]))
