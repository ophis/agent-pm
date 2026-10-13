"""Bundled tmux grids operations, independent of the standalone facade."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import runpy
import subprocess
import sys
import time
from typing import TYPE_CHECKING

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux import anchors as _anchors
    from agent_pm.session.hosts.tmux import commands as _commands
    from agent_pm.session.hosts.tmux import layout as _layout
    from agent_pm.session.hosts.tmux import validation as _validation
    from agent_pm.session.hosts.tmux.errors import types as _errors
    from agent_pm.session.hosts.tmux.model import types as _model
else:
    load_siblings = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_siblings"]
    _anchors, _commands, _errors, _layout, _model, _validation = load_siblings(
        DIRECTORY,
        ("agent_pm.session.hosts.tmux.anchors", "anchors.py"),
        ("agent_pm.session.hosts.tmux.commands", "commands.py"),
        ("agent_pm.session.hosts.tmux.errors.types", "errors/types.py"),
        ("agent_pm.session.hosts.tmux.layout", "layout.py"),
        ("agent_pm.session.hosts.tmux.model.types", "model/types.py"),
        ("agent_pm.session.hosts.tmux.validation", "validation.py"),
    )


def find_grid(
    opener: str, sessions: list[list[str]], proc, *, own: bool | None
) -> tuple[str, str, _model.Anchor, str] | None:
    """(grid window, manager, pane to split, side) when the opener is in a grid or a root a client
    shows; None: iTerm2 mode. It splits the largest pane but the manager below, else the manager
    right.
    """
    row = next((f for f in sessions if f[1] == opener), None)
    if row is None:
        return None
    try:
        found = _anchors.pane_window(row[3], None, proc) if _model.PANE.fullmatch(row[3]) else None
        if found is None or found[1] is None:
            clients = [] if _model.OPENER.fullmatch(row[2]) else _anchors.list_clients(opener, proc)
            if not clients:
                return None
            if own is None:
                own = _anchors.own_session(proc=proc) == opener
            pane = os.environ["TMUX_PANE"] if own else clients[0][2]
            found = _anchors.pane_window(pane, pane, proc)
    except _errors.TuiError:
        return None
    if found is None:
        return None
    window, manager, areas, socket = found
    assert manager is not None
    workers = [p for p in areas if p != manager]
    if not workers:
        return window, manager, _model.Anchor(None, manager, socket), "right"
    return (
        window,
        manager,
        _model.Anchor(None, max(workers, key=lambda p: (areas[p], int(p[1:]))), socket),
        "below",
    )


def open_grid(
    window: str,
    manager: str,
    settings: _model.GridSettings,
    services: _model.PaneServices,
) -> str | None:
    """Sets the grid options and retile's hooks (none when tmux would misread a path), then tiles
    unless off. None or the failure.
    """
    per_column, retile, proc = settings.per_column, settings.retile, services.proc
    args = [
        "set-option",
        "-w",
        "-t",
        window,
        _model.GRID_MANAGER,
        manager,
        ";",
        "set-option",
        "-w",
        "-t",
        window,
        _model.GRID_PER_COLUMN,
        str(per_column),
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        window,
        _model.GRID_BUSY,
    ]
    mode = _model.RETILE_HOOKS[retile]
    paths = (sys.executable or "", os.path.abspath(services.script))
    bad = [p for p in paths if not _model.HOOK_PATH.fullmatch(p)] if mode else []
    if bad:
        print(f"tui: show: grid: hooks: {bad[0]}: tmux would misread it", file=sys.stderr)
    flags = "--no-wait --if-panes-changed" if retile == "open-close" else "--no-wait"
    hook = (
        f"if-shell -F '#{{==:#{{{_model.GRID_BUSY}}},{window}}}' '' "
        f"\"run-shell -b '{paths[0]} -I {paths[1]} tile {flags} {window} >/dev/null 2>&1 || true'\""
    )
    for name in _model.GRID_HOOKS:
        if name not in mode:
            args += [";", "set-hook", "-u", "-w", "-t", window, name]
        elif not bad:
            args += [";", "set-hook", "-w", "-t", window, name, hook]
    try:
        _commands.checked_tmux(args, proc)
        if mode:
            (tile if services.tile_window is None else services.tile_window)(
                window, per_column=per_column, proc=proc
            )
    except _errors.TuiError as e:
        return str(e)
    return None


def remove_grid(window: str, proc) -> None:
    options = (
        _model.GRID_MANAGER,
        _model.GRID_PER_COLUMN,
        _model.GRID_TILES,
        _model.GRID_BUSY,
        _model.GRID_PANES_SET,
    )
    args = [
        a
        for command, names in (("set-hook", _model.GRID_HOOKS), ("set-option", options))
        for name in names
        for a in (";", command, "-u", "-w", "-t", window, name)
    ]
    _commands.checked_tmux(args[1:], proc)


def tile(
    window: str,
    *,
    per_column: int | None = None,
    wait: bool = True,
    if_panes_changed: bool = False,
    proc=subprocess.run,
) -> None:
    """Lays out grid window `window` under its _grid_lock (no wait: nothing while another tile holds
    it), its hooks muted by @grid-busy. if_panes_changed: no pass on an unchanged pane set. Over
    TILE_LIMIT re-tiles in TILE_WINDOW s, the grid is torn down.
    """
    if not _model.WINDOW.fullmatch(window):
        raise _errors.TuiError.invalid_window(window, _model.WINDOW.pattern)
    _validation.validate_per_column(per_column)
    socket = _commands.checked_tmux(
        ["display-message", "-p", "-t", window, "#{socket_path}"], proc
    ).stdout.rstrip("\n")
    with grid_lock(socket, window, wait=wait) as held:
        found = (
            grid_pass(window, per_column, proc, if_panes_changed=if_panes_changed) if held else None
        )
        if found is None:
            return
        swaps, layout, tiles, panes = found
        if layout is None:
            _commands.checked_tmux(
                ["set-option", "-w", "-t", window, _model.GRID_PANES_SET, panes], proc
            )
            return
        now = int(time.monotonic() * 1000)
        times = [
            t
            for t in (int(s) for s in tiles.split() if re.fullmatch(r"[0-9]{1,15}", s))
            if now - _model.TILE_WINDOW * 1000 < t <= now
        ] + [now]
        if len(times) > _model.TILE_LIMIT:
            remove_grid(window, proc)
            line = (
                f"tui: grid "
                f"{window}"
                f": auto re-tiling stopped: "
                f"{len(times)}"
                f" re-tiles in "
                f"{_model.TILE_WINDOW}"
                f" s; the next worker that opens rebuilds "
                f"it"
            )
            with contextlib.suppress(_errors.TuiError):
                _commands.call_tmux(["display-message", "-d", "10000", "-t", window, line], proc)
            print(line, file=sys.stderr)
            return
        try:
            _commands.checked_tmux(
                [
                    "set-option",
                    "-w",
                    "-t",
                    window,
                    _model.GRID_TILES,
                    " ".join(map(str, times)),
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    window,
                    _model.GRID_PANES_SET,
                    panes,
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    window,
                    _model.GRID_BUSY,
                    window,
                    ";",
                    *(
                        a
                        for source, target in swaps
                        for a in ("swap-pane", "-d", "-s", source, "-t", target, ";")
                    ),
                    "select-layout",
                    "-t",
                    window,
                    _layout.render_layout(layout),
                ],
                proc,
            )
        finally:
            with contextlib.suppress(_errors.TuiError):
                _commands.call_tmux(
                    ["set-option", "-u", "-w", "-t", window, _model.GRID_BUSY], proc
                )


def grid_pass(
    window: str, per_column: int | None, proc, *, if_panes_changed: bool
) -> tuple[list[tuple[str, str]], _model.Cell | None, str, str] | None:
    """tile's pass: (swaps, layout, @grid-tiles, pane set) or None (zoomed, no grid, unreadable
    layout, unchanged set). Up to SETTLE_ITERATIONS more passes run while the result changes: the
    hooks are muted.
    """
    fields = (
        _commands.checked_tmux(["display-message", "-p", "-t", window, _model.GRID_WINDOW], proc)
        .stdout.rstrip("\n")
        .split("\t", 7)
    )
    if len(fields) != 8 or fields[2] != "0" or not fields[4]:
        return None
    width, height, _, layout, manager, n, recorded, tiles = fields
    panes = _commands.checked_tmux(
        ["list-panes", "-t", window, "-F", "#{pane_id}"], proc
    ).stdout.split()
    current = _layout.parse_layout(layout)
    leaves = [] if current is None else _layout.layout_leaves(current)
    if current is None or sorted(_layout.leaf_pane(leaf) for leaf in leaves) != sorted(panes):
        return None
    if manager not in panes or len(panes) == 1:
        remove_grid(window, proc)
        return None
    pane_set = " ".join(sorted(panes, key=lambda p: int(p[1:])))
    recorded = recorded if _model.PANE_SET.fullmatch(recorded) else ""
    if if_panes_changed and recorded == pane_set:
        return None
    sessions = grid_sessions(proc)
    if per_column is None:
        per_column = int(n) if re.fullmatch(r"[1-9][0-9]{0,3}", n) else _model.PER_COLUMN
    order, at = panes, current
    for _ in range(1 + _model.SETTLE_ITERATIONS):
        want = _layout.grid_layout(
            _layout.grid_tree(manager, order, sessions, at),
            manager,
            geometry=_model.GridGeometry(
                width=int(width),
                height=int(height),
                manager_width=next(c.w for c in _layout.layout_leaves(at) if c.pane == manager),
                per_column=per_column,
            ),
        )
        if want is None or _layout.layout_leaves(want) == _layout.layout_leaves(at):
            break
        order, at = [_layout.leaf_pane(c) for c in _layout.layout_leaves(want)], want
    if _layout.layout_leaves(at) == leaves:
        return None if recorded == pane_set else ([], None, tiles, pane_set)
    return _layout.pane_swaps(panes, order), at, tiles, pane_set


@contextlib.contextmanager
def grid_lock(socket: str, window: str, *, wait: bool):
    """Yields whether it holds the window's flock (<socket>-<n>.lock); wait: retries every TILE_POLL
    s, TuiError after TILE_WAIT s.
    """
    if not os.path.isabs(socket):
        raise _errors.TuiError.invalid_grid_socket(window, socket)
    path = f"{socket}-{window[1:]}.lock"
    try:
        fd = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600
        )
    except OSError as e:
        raise _errors.TuiError.grid_lock_failed(path, e) from e
    try:
        end = time.monotonic() + _model.TILE_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:
                held = False
            except OSError as e:
                raise _errors.TuiError.grid_lock_failed(path, e) from e
            if held or not wait:
                break
            if time.monotonic() >= end:
                raise _errors.TuiError.busy_grid(window)
            time.sleep(_model.TILE_POLL)
        yield held
    finally:
        os.close(fd)


def grid_sessions(proc):
    sessions = []
    for f in (
        line.split("\t")
        for line in _commands.checked_tmux(
            ["list-sessions", "-F", _model.SESSIONS], proc
        ).stdout.split("\n")
    ):
        if len(f) == 5 and _model.SESSION_ID.fullmatch(f[0]) and _model.NAME.fullmatch(f[1]):
            sessions.append(
                (
                    int(f[0][1:]),
                    f[1],
                    f[2] if _model.OPENER.fullmatch(f[2]) else "",
                    f[3] if _model.PANE.fullmatch(f[3]) else "",
                )
            )
    return sessions
