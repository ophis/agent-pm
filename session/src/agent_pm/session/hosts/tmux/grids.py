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
        _clients,
        _window,
        own_session,
    )
    from agent_pm.session.hosts.tmux.commands import (
        _tmux,
        _tmux_ok,
    )
    from agent_pm.session.hosts.tmux.errors.types import (
        TuiError,
    )
    from agent_pm.session.hosts.tmux.layout import (
        _grid_layout,
        _grid_tree,
        _leaf_pane,
        _leaves,
        _parse_layout,
        _render_layout,
        _swaps,
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
        _per_column,
    )
else:
    _load_file = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_file"]
    _anchors = _load_file(
        "agent_pm.session.hosts.tmux.anchors", os.path.join(DIRECTORY, "anchors.py")
    )
    _clients = _anchors._clients
    _window = _anchors._window
    own_session = _anchors.own_session
    _commands = _load_file(
        "agent_pm.session.hosts.tmux.commands", os.path.join(DIRECTORY, "commands.py")
    )
    _tmux = _commands._tmux
    _tmux_ok = _commands._tmux_ok
    _errors_types = _load_file(
        "agent_pm.session.hosts.tmux.errors.types", os.path.join(DIRECTORY, "errors/types.py")
    )
    TuiError = _errors_types.TuiError
    _layout = _load_file("agent_pm.session.hosts.tmux.layout", os.path.join(DIRECTORY, "layout.py"))
    _grid_layout = _layout._grid_layout
    _grid_tree = _layout._grid_tree
    _leaf_pane = _layout._leaf_pane
    _leaves = _layout._leaves
    _parse_layout = _layout._parse_layout
    _render_layout = _layout._render_layout
    _swaps = _layout._swaps
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
    _per_column = _validation._per_column


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
        if found is None or found[1] is None:
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


def _grid_up(
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
        _tmux_ok(args, proc)
        if mode:
            (tile if services.tile_window is None else services.tile_window)(
                window, per_column=per_column, proc=proc
            )
    except TuiError as e:
        return str(e)
    return None


def _grid_down(window: str, proc) -> None:
    options = (GRID_MANAGER, GRID_PER_COLUMN, GRID_TILES, GRID_BUSY, GRID_PANES_SET)
    args = [
        a
        for command, names in (("set-hook", GRID_HOOKS), ("set-option", options))
        for name in names
        for a in (";", command, "-u", "-w", "-t", window, name)
    ]
    _tmux_ok(args[1:], proc)


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
    _per_column(per_column)
    socket = _tmux_ok(
        ["display-message", "-p", "-t", window, "#{socket_path}"], proc
    ).stdout.rstrip("\n")
    with _grid_lock(socket, window, wait=wait) as held:
        found = (
            _grid_pass(window, per_column, proc, if_panes_changed=if_panes_changed)
            if held
            else None
        )
        if found is None:
            return
        swaps, layout, tiles, panes = found
        if layout is None:
            _tmux_ok(["set-option", "-w", "-t", window, GRID_PANES_SET, panes], proc)
            return
        now = int(time.monotonic() * 1000)
        times = [
            t
            for t in (int(s) for s in tiles.split() if re.fullmatch(r"[0-9]{1,15}", s))
            if now - TILE_WINDOW * 1000 < t <= now
        ] + [now]
        if len(times) > TILE_LIMIT:
            _grid_down(window, proc)
            line = (
                f"tui: grid {window}: auto re-tiling stopped: {len(times)} re-tiles in {TILE_WINDOW} s; the next "
                "worker that opens rebuilds it"
            )
            with contextlib.suppress(TuiError):
                _tmux(["display-message", "-d", "10000", "-t", window, line], proc)
            print(line, file=sys.stderr)
            return
        try:
            _tmux_ok(
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
                    _render_layout(layout),
                ],
                proc,
            )
        finally:
            with contextlib.suppress(TuiError):
                _tmux(["set-option", "-u", "-w", "-t", window, GRID_BUSY], proc)


def _grid_pass(
    window: str, per_column: int | None, proc, *, if_panes_changed: bool
) -> tuple[list[tuple[str, str]], Cell | None, str, str] | None:
    """tile's pass: (swaps, layout, @grid-tiles, pane set) or None (zoomed, no grid, unreadable
    layout, unchanged set). Up to SETTLE_ITERATIONS more passes run while the result changes: the
    hooks are muted.
    """
    fields = (
        _tmux_ok(["display-message", "-p", "-t", window, GRID_WINDOW], proc)
        .stdout.rstrip("\n")
        .split("\t", 7)
    )
    if len(fields) != 8 or fields[2] != "0" or not fields[4]:
        return None
    width, height, _, layout, manager, n, recorded, tiles = fields
    panes = _tmux_ok(["list-panes", "-t", window, "-F", "#{pane_id}"], proc).stdout.split()
    current = _parse_layout(layout)
    leaves = [] if current is None else _leaves(current)
    if current is None or sorted(_leaf_pane(leaf) for leaf in leaves) != sorted(panes):
        return None
    if manager not in panes or len(panes) == 1:
        _grid_down(window, proc)
        return None
    pane_set = " ".join(sorted(panes, key=lambda p: int(p[1:])))
    recorded = recorded if PANE_SET.fullmatch(recorded) else ""
    if if_panes_changed and recorded == pane_set:
        return None
    sessions = []
    for f in (
        line.split("\t")
        for line in _tmux_ok(["list-sessions", "-F", SESSIONS], proc).stdout.split("\n")
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
    if per_column is None:
        per_column = int(n) if re.fullmatch(r"[1-9][0-9]{0,3}", n) else PER_COLUMN
    order, at = panes, current
    for _ in range(1 + SETTLE_ITERATIONS):
        want = _grid_layout(
            _grid_tree(manager, order, sessions, at),
            manager,
            geometry=GridGeometry(
                width=int(width),
                height=int(height),
                manager_width=next(c.w for c in _leaves(at) if c.pane == manager),
                per_column=per_column,
            ),
        )
        if want is None or _leaves(want) == _leaves(at):
            break
        order, at = [_leaf_pane(c) for c in _leaves(want)], want
    if _leaves(at) == leaves:
        return None if recorded == pane_set else ([], None, tiles, pane_set)
    return _swaps(panes, order), at, tiles, pane_set


@contextlib.contextmanager
def _grid_lock(socket: str, window: str, *, wait: bool):
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
