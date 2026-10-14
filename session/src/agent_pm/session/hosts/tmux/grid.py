"""Tmux grid mutation, retile hooks and serialization."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time

import agent_pm.utils
from agent_pm.session.hosts.tmux.layout import (
    grid_layout,
    grid_tree,
    leaf_cells,
    pane_ids,
    parse_layout,
    render_layout,
    swaps,
)
from agent_pm.session.hosts.tmux.transport import (
    NAME,
    OPENER,
    PANE,
    SESSION_ID,
    SESSIONS,
    checked,
    run,
)
from agent_pm.session.hosts.tmux.types import (
    Cell,
    Retile,
    TuiError,
)
from agent_pm.utils import module_roots

WINDOW = re.compile(r"@[0-9]+")


ENTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "entry.py")


def entry_roots() -> str:
    roots = module_roots(sys.modules[__name__], agent_pm.utils)
    return base64.urlsafe_b64encode(json.dumps(roots).encode()).decode()


PER_COLUMN = 3


GRID_MANAGER = "@grid-manager"


GRID_PER_COLUMN = "@grid-per-column"


GRID_TILES = "@grid-tiles"


GRID_BUSY = "@grid-busy"


GRID_PANES_SET = "@grid-panes"  # sorted pane ids at the last tile


PANE_SET = re.compile(r"%[0-9]+( %[0-9]+)*")


GRID_HOOKS = ("pane-exited", "window-resized", "window-layout-changed")


RETILE = Retile.OPEN_CLOSE


RETILE_HOOKS: dict[Retile, tuple[str, ...]] = {
    Retile.ALL: GRID_HOOKS,
    Retile.OPEN_CLOSE: ("pane-exited", "window-layout-changed"),
    Retile.OFF: (),
}


GRID_WINDOW = (
    "#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{window_layout}\t"
    f"#{{{GRID_MANAGER}}}\t#{{{GRID_PER_COLUMN}}}\t#{{{GRID_PANES_SET}}}\t#{{{GRID_TILES}}}"
)


GRID_PANES = f"#{{window_id}}\t#{{{GRID_MANAGER}}}\t#{{pane_id}}\t#{{pane_width}}\t#{{pane_height}}\t#{{socket_path}}"


GRID_ROW = re.compile(r"(@[0-9]+)\t([^\t]*)\t(%[0-9]+)\t([0-9]+)\t([0-9]+)\t([^\t]+)")


HOOK_PATH = re.compile(r"[A-Za-z0-9_./@+-]+")


TILE_WAIT = 5


TILE_POLL = 0.05


TILE_LIMIT = 50  # re-tiles per TILE_WINDOW s; one more stops auto re-tiling


TILE_WINDOW = 10


SETTLE_ITERATIONS = 3


def configure(
    window: str,
    manager: str,
    *,
    per_column: int,
    retile: Retile,
    script: str = ENTRY,
    proc=subprocess.run,
    tile_window=None,
) -> str | None:
    """Sets the grid options and retile's hooks (none when tmux would misread a path), then tiles
    unless off. None or the failure.
    """
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
    paths = (sys.executable or "", os.path.abspath(script))
    bad = [p for p in paths if not HOOK_PATH.fullmatch(p)] if mode else []
    if bad:
        print(f"tui: show: grid: hooks: {bad[0]}: tmux would misread it", file=sys.stderr)
    flags = "--no-wait --if-panes-changed" if retile == Retile.OPEN_CLOSE else "--no-wait"
    hook = (
        f"if-shell -F '#{{==:#{{{GRID_BUSY}}},{window}}}' '' "
        f"\"run-shell -b '{paths[0]} -I {paths[1]} {entry_roots()} tile {flags} {window} >/dev/null 2>&1 || true'\""
    )
    for name in GRID_HOOKS:
        if name not in mode:
            args += [";", "set-hook", "-u", "-w", "-t", window, name]
        elif not bad:
            args += [";", "set-hook", "-w", "-t", window, name, hook]
    try:
        checked(args, proc)
        if mode:
            (tile if tile_window is None else tile_window)(window, per_column=per_column, proc=proc)
    except TuiError as e:
        return str(e)
    return None


def _grid_down(window: str, proc) -> None:
    options = (
        GRID_MANAGER,
        GRID_PER_COLUMN,
        GRID_TILES,
        GRID_BUSY,
        GRID_PANES_SET,
    )
    args = [
        a
        for command, names in (("set-hook", GRID_HOOKS), ("set-option", options))
        for name in names
        for a in (";", command, "-u", "-w", "-t", window, name)
    ]
    checked(args[1:], proc)


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
    TILE_LIMIT re-tiles in TILE_WINDOW s, the grid is torn down. Holding the lock, every exit unsets
    @grid-busy, a stale one too; without it, tile leaves @grid-busy alone.
    """
    if not WINDOW.fullmatch(window):
        raise TuiError(f"invalid window id {window!r}: want {WINDOW.pattern}")
    validate_per_column(per_column)
    socket = checked(["display-message", "-p", "-t", window, "#{socket_path}"], proc).stdout.rstrip(
        "\n"
    )
    with _grid_lock(socket, window, wait=wait) as held:
        if not held:
            return
        try:
            found = _grid_pass(window, per_column, proc, if_panes_changed=if_panes_changed)
            if found is None:
                return
            swaps, layout, tiles, panes = found
            if layout is None:
                checked(["set-option", "-w", "-t", window, GRID_PANES_SET, panes], proc)
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
                    run(["display-message", "-d", "10000", "-t", window, line], proc)
                print(line, file=sys.stderr)
                return
            checked(
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
                run(["set-option", "-u", "-w", "-t", window, GRID_BUSY], proc)


def _grid_pass(
    window: str, per_column: int | None, proc, *, if_panes_changed: bool
) -> tuple[list[tuple[str, str]], Cell | None, str, str] | None:
    """tile's pass: (swaps, layout, @grid-tiles, pane set) or None (zoomed, no grid, unreadable
    layout, unchanged set). Up to SETTLE_ITERATIONS more passes run while the result changes: the
    hooks are muted.
    """
    fields = (
        checked(["display-message", "-p", "-t", window, GRID_WINDOW], proc)
        .stdout.rstrip("\n")
        .split("\t", 7)
    )
    if not fields[0]:  # tmux formats a missing window's fields empty, exit 0
        raise TuiError(f"can't find window: {window}")
    if len(fields) != 8 or fields[2] != "0" or not fields[4]:
        return None
    width, height, _, layout, manager, n, recorded, tiles = fields
    panes = checked(["list-panes", "-t", window, "-F", "#{pane_id}"], proc).stdout.split()
    current = parse_layout(layout)
    leaves = [] if current is None else leaf_cells(current)
    if current is None or sorted(pane_ids(current)) != sorted(panes):
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
        for line in checked(["list-sessions", "-F", SESSIONS], proc).stdout.split("\n")
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
        want = grid_layout(
            grid_tree(manager, order, sessions, at),
            manager,
            width=int(width),
            height=int(height),
            manager_width=next(c.w for c in leaf_cells(at) if c.pane == manager),
            per_column=per_column,
        )
        if want is None or leaf_cells(want) == leaf_cells(at):
            break
        order, at = pane_ids(want), want
    if leaf_cells(at) == leaves:
        return None if recorded == pane_set else ([], None, tiles, pane_set)
    return swaps(panes, order), at, tiles, pane_set


@contextlib.contextmanager
def _grid_lock(socket: str, window: str, *, wait: bool):
    """Yields whether it holds the window's flock (<socket>-<n>.lock); wait: retries every TILE_POLL
    s, TuiError after TILE_WAIT s.
    """
    if not os.path.isabs(socket):
        raise TuiError(f"grid {window}: socket path {socket!r}: want an absolute path")
    path = f"{socket}-{window[1:]}.lock"
    try:
        fd = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600
        )
    except OSError as e:
        raise TuiError(f"grid lock {path}: {e.strerror}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise TuiError(f"grid lock {path}: not a regular file owned by you")
        end = time.monotonic() + TILE_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:
                held = False
            except OSError as e:
                raise TuiError(f"grid lock {path}: {e.strerror}") from e
            if held or not wait:
                break
            if time.monotonic() >= end:
                raise TuiError(f"grid {window}: busy")
            time.sleep(TILE_POLL)
        yield held
    finally:
        os.close(fd)


def validate_per_column(per_column: int | None) -> None:
    if per_column is not None and (
        isinstance(per_column, bool)
        or not isinstance(per_column, int)
        or not 1 <= per_column <= 9999
    ):
        raise TuiError(f"per_column must be an int from 1 to 9999, not {per_column!r}")


def validate_retile(retile: str | None) -> None:
    if retile is not None and retile not in tuple(Retile):
        raise TuiError(f"retile must be one of {', '.join(Retile)}, not {retile!r}")
