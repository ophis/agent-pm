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
    from agent_pm.session.hosts.tmux.anchors import (
        list_clients,
        own_session,
        pane_window,
    )
    from agent_pm.session.hosts.tmux.commands import (
        call_tmux,
        checked_tmux,
    )
    from agent_pm.session.hosts.tmux.errors.types import (
        TuiError,
    )
    from agent_pm.session.hosts.tmux.layout import (
        grid_layout,
        grid_tree,
        layout_leaves,
        leaf_pane,
        pane_swaps,
        parse_layout,
        render_layout,
    )
    from agent_pm.session.hosts.tmux.model.types import (
        GRID_BUSY,
        GRID_HOOKS,
        GRID_MANAGER,
        GRID_PANES_SET,
        GRID_PER_COLUMN,
        GRID_TILES,
        GRID_WINDOW,
        HOOK_PATH,
        NAME,
        OPENER,
        PANE,
        PANE_SET,
        PER_COLUMN,
        RETILE_HOOKS,
        SESSION_ID,
        SESSIONS,
        SETTLE_ITERATIONS,
        TILE_LIMIT,
        TILE_POLL,
        TILE_WAIT,
        TILE_WINDOW,
        WINDOW,
        Anchor,
        Cell,
        GridGeometry,
        GridSettings,
        PaneServices,
    )
    from agent_pm.session.hosts.tmux.validation import (
        validate_per_column,
    )
else:
    _load_file = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_file"]
    _anchors = _load_file(
        "agent_pm.session.hosts.tmux.anchors", os.path.join(DIRECTORY, "anchors.py")
    )
    list_clients = _anchors.list_clients
    own_session = _anchors.own_session
    pane_window = _anchors.pane_window
    _commands = _load_file(
        "agent_pm.session.hosts.tmux.commands", os.path.join(DIRECTORY, "commands.py")
    )
    call_tmux = _commands.call_tmux
    checked_tmux = _commands.checked_tmux
    _errors_types = _load_file(
        "agent_pm.session.hosts.tmux.errors.types", os.path.join(DIRECTORY, "errors/types.py")
    )
    TuiError = _errors_types.TuiError
    _layout = _load_file("agent_pm.session.hosts.tmux.layout", os.path.join(DIRECTORY, "layout.py"))
    grid_layout = _layout.grid_layout
    grid_tree = _layout.grid_tree
    layout_leaves = _layout.layout_leaves
    leaf_pane = _layout.leaf_pane
    pane_swaps = _layout.pane_swaps
    parse_layout = _layout.parse_layout
    render_layout = _layout.render_layout
    _model_types = _load_file(
        "agent_pm.session.hosts.tmux.model.types", os.path.join(DIRECTORY, "model/types.py")
    )
    Anchor = _model_types.Anchor
    Cell = _model_types.Cell
    GRID_BUSY = _model_types.GRID_BUSY
    GRID_HOOKS = _model_types.GRID_HOOKS
    GRID_MANAGER = _model_types.GRID_MANAGER
    GRID_PANES_SET = _model_types.GRID_PANES_SET
    GRID_PER_COLUMN = _model_types.GRID_PER_COLUMN
    GRID_TILES = _model_types.GRID_TILES
    GRID_WINDOW = _model_types.GRID_WINDOW
    GridGeometry = _model_types.GridGeometry
    GridSettings = _model_types.GridSettings
    HOOK_PATH = _model_types.HOOK_PATH
    NAME = _model_types.NAME
    OPENER = _model_types.OPENER
    PANE = _model_types.PANE
    PANE_SET = _model_types.PANE_SET
    PER_COLUMN = _model_types.PER_COLUMN
    PaneServices = _model_types.PaneServices
    RETILE_HOOKS = _model_types.RETILE_HOOKS
    SESSIONS = _model_types.SESSIONS
    SESSION_ID = _model_types.SESSION_ID
    SETTLE_ITERATIONS = _model_types.SETTLE_ITERATIONS
    TILE_LIMIT = _model_types.TILE_LIMIT
    TILE_POLL = _model_types.TILE_POLL
    TILE_WAIT = _model_types.TILE_WAIT
    TILE_WINDOW = _model_types.TILE_WINDOW
    WINDOW = _model_types.WINDOW
    _validation = _load_file(
        "agent_pm.session.hosts.tmux.validation", os.path.join(DIRECTORY, "validation.py")
    )
    validate_per_column = _validation.validate_per_column


def find_grid(
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
        found = pane_window(row[3], None, proc) if PANE.fullmatch(row[3]) else None
        if found is None or found[1] is None:
            clients = [] if OPENER.fullmatch(row[2]) else list_clients(opener, proc)
            if not clients:
                return None
            if own is None:
                own = own_session(proc=proc) == opener
            pane = os.environ["TMUX_PANE"] if own else clients[0][2]
            found = pane_window(pane, pane, proc)
    except TuiError:
        return None
    if found is None:
        return None
    window, manager, areas, socket = found
    assert manager is not None
    workers = [p for p in areas if p != manager]
    if not workers:
        return window, manager, Anchor(None, manager, socket), "right"
    return (
        window,
        manager,
        Anchor(None, max(workers, key=lambda p: (areas[p], int(p[1:]))), socket),
        "below",
    )


def open_grid(
    window: str, manager: str, settings: GridSettings, services: PaneServices
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
        GRID_MANAGER,
        manager,
        ";",
        "set-option",
        "-w",
        "-t",
        window,
        GRID_PER_COLUMN,
        str(per_column),
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        window,
        GRID_BUSY,
    ]
    mode = RETILE_HOOKS[retile]
    paths = (sys.executable or "", os.path.abspath(services.script))
    bad = [p for p in paths if not HOOK_PATH.fullmatch(p)] if mode else []
    if bad:
        print(f"tui: show: grid: hooks: {bad[0]}: tmux would misread it", file=sys.stderr)
    flags = "--no-wait --if-panes-changed" if retile == "open-close" else "--no-wait"
    hook = (
        f"if-shell -F '#{{==:#{{{GRID_BUSY}}},{window}}}' '' "
        f"\"run-shell -b '{paths[0]} -I {paths[1]} tile {flags} {window} >/dev/null 2>&1 || true'\""
    )
    for name in GRID_HOOKS:
        if name not in mode:
            args += [";", "set-hook", "-u", "-w", "-t", window, name]
        elif not bad:
            args += [";", "set-hook", "-w", "-t", window, name, hook]
    try:
        checked_tmux(args, proc)
        if mode:
            (tile if services.tile_window is None else services.tile_window)(
                window, per_column=per_column, proc=proc
            )
    except TuiError as e:
        return str(e)
    return None


def remove_grid(window: str, proc) -> None:
    options = (GRID_MANAGER, GRID_PER_COLUMN, GRID_TILES, GRID_BUSY, GRID_PANES_SET)
    args = [
        a
        for command, names in (("set-hook", GRID_HOOKS), ("set-option", options))
        for name in names
        for a in (";", command, "-u", "-w", "-t", window, name)
    ]
    checked_tmux(args[1:], proc)


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
    if not WINDOW.fullmatch(window):
        raise TuiError.invalid_window(window, WINDOW.pattern)
    validate_per_column(per_column)
    socket = checked_tmux(
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
            checked_tmux(["set-option", "-w", "-t", window, GRID_PANES_SET, panes], proc)
            return
        now = int(time.monotonic() * 1000)
        times = [
            t
            for t in (int(s) for s in tiles.split() if re.fullmatch(r"[0-9]{1,15}", s))
            if now - TILE_WINDOW * 1000 < t <= now
        ] + [now]
        if len(times) > TILE_LIMIT:
            remove_grid(window, proc)
            line = (
                f"tui: grid "
                f"{window}"
                f": auto re-tiling stopped: "
                f"{len(times)}"
                f" re-tiles in "
                f"{TILE_WINDOW}"
                f" s; the next worker that opens rebuilds "
                f"it"
            )
            with contextlib.suppress(TuiError):
                call_tmux(["display-message", "-d", "10000", "-t", window, line], proc)
            print(line, file=sys.stderr)
            return
        try:
            checked_tmux(
                [
                    "set-option",
                    "-w",
                    "-t",
                    window,
                    GRID_TILES,
                    " ".join(map(str, times)),
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    window,
                    GRID_PANES_SET,
                    panes,
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    window,
                    GRID_BUSY,
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
                    render_layout(layout),
                ],
                proc,
            )
        finally:
            with contextlib.suppress(TuiError):
                call_tmux(["set-option", "-u", "-w", "-t", window, GRID_BUSY], proc)


def grid_pass(
    window: str, per_column: int | None, proc, *, if_panes_changed: bool
) -> tuple[list[tuple[str, str]], Cell | None, str, str] | None:
    """tile's pass: (swaps, layout, @grid-tiles, pane set) or None (zoomed, no grid, unreadable
    layout, unchanged set). Up to SETTLE_ITERATIONS more passes run while the result changes: the
    hooks are muted.
    """
    fields = (
        checked_tmux(["display-message", "-p", "-t", window, GRID_WINDOW], proc)
        .stdout.rstrip("\n")
        .split("\t", 7)
    )
    if len(fields) != 8 or fields[2] != "0" or not fields[4]:
        return None
    width, height, _, layout, manager, n, recorded, tiles = fields
    panes = checked_tmux(["list-panes", "-t", window, "-F", "#{pane_id}"], proc).stdout.split()
    current = parse_layout(layout)
    leaves = [] if current is None else layout_leaves(current)
    if current is None or sorted(leaf_pane(leaf) for leaf in leaves) != sorted(panes):
        return None
    if manager not in panes or len(panes) == 1:
        remove_grid(window, proc)
        return None
    pane_set = " ".join(sorted(panes, key=lambda p: int(p[1:])))
    recorded = recorded if PANE_SET.fullmatch(recorded) else ""
    if if_panes_changed and recorded == pane_set:
        return None
    sessions = grid_sessions(proc)
    if per_column is None:
        per_column = int(n) if re.fullmatch(r"[1-9][0-9]{0,3}", n) else PER_COLUMN
    order, at = panes, current
    for _ in range(1 + SETTLE_ITERATIONS):
        want = grid_layout(
            grid_tree(manager, order, sessions, at),
            manager,
            geometry=GridGeometry(
                width=int(width),
                height=int(height),
                manager_width=next(c.w for c in layout_leaves(at) if c.pane == manager),
                per_column=per_column,
            ),
        )
        if want is None or layout_leaves(want) == layout_leaves(at):
            break
        order, at = [leaf_pane(c) for c in layout_leaves(want)], want
    if layout_leaves(at) == leaves:
        return None if recorded == pane_set else ([], None, tiles, pane_set)
    return pane_swaps(panes, order), at, tiles, pane_set


@contextlib.contextmanager
def grid_lock(socket: str, window: str, *, wait: bool):
    """Yields whether it holds the window's flock (<socket>-<n>.lock); wait: retries every TILE_POLL
    s, TuiError after TILE_WAIT s.
    """
    if not os.path.isabs(socket):
        raise TuiError.invalid_grid_socket(window, socket)
    path = f"{socket}-{window[1:]}.lock"
    try:
        fd = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600
        )
    except OSError as e:
        raise TuiError.grid_lock_failed(path, e) from e
    try:
        end = time.monotonic() + TILE_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:
                held = False
            except OSError as e:
                raise TuiError.grid_lock_failed(path, e) from e
            if held or not wait:
                break
            if time.monotonic() >= end:
                raise TuiError.busy_grid(window)
            time.sleep(TILE_POLL)
        yield held
    finally:
        os.close(fd)


def grid_sessions(proc):
    sessions = []
    for f in (
        line.split("\t")
        for line in checked_tmux(["list-sessions", "-F", SESSIONS], proc).stdout.split("\n")
    ):
        if len(f) == 5 and SESSION_ID.fullmatch(f[0]) and NAME.fullmatch(f[1]):
            sessions.append(
                (
                    int(f[0][1:]),
                    f[1],
                    f[2] if OPENER.fullmatch(f[2]) else "",
                    f[3] if PANE.fullmatch(f[3]) else "",
                )
            )
    return sessions
