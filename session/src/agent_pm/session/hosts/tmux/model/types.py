"""Tmux protocol facts and execution, placement and layout records for bundled helpers."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

NAME = re.compile(r"[A-Za-z0-9_-]+")


PANE = re.compile(r"%[0-9]+")


ITERM_ID = re.compile(r"[A-Za-z0-9-]+")


ITERM_OPENER = re.compile(rf"[A-Za-z0-9]+:{ITERM_ID.pattern}")  # w0t0p0:<unique id>


OPENER = re.compile(rf"{NAME.pattern}|{ITERM_OPENER.pattern}")


SESSION_ID = re.compile(r"\$[0-9]+")


USER_OPTION = re.compile(r"@[A-Za-z0-9_-]+")


ABSENT = re.compile(
    r"can't find (session|pane).*|no server running.*|"
    r"error connecting to .* \(No such file or directory\)",
    re.S,
)


HOST = "tmux"  # its [hosts.<name>] table


RUNNING = "running"


UNKNOWN = "unknown"  # tmux failed otherwise, or is missing


HANDOVER_TIMEOUT = 10


ERRPIPE = "errpipe"


POLL = 0.1


PAUSE = 0.5


SHOW_TIMEOUT = 30


MARK_TIMEOUT = 5


PLACEHOLDERS = {"session"}


SLOT = re.compile(r"\{\{([^{}]*)\}\}")


SPLITS = ("right", "below")


DIED = "set-option @state dead"


DEAD_EVENT = "run-shell -b 'echo \"$(date +%H:%M:%S)\" #{q:session_name} dead >> #{q:@events}'"


BORDER = " #{session_name} #{@state} "


CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"


PANES = "#{pane_dead} #{pane_tty} #{pane_id}"


SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"


LISTING = "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@runtime}\t#{@state}\t#{@pane}\t#{@opener}"


SHOWN = "#{client_activity}\t#{client_tty}\t#{session_name}"


HOSTS = "#{pane_dead}\t#{pane_tty}\t#{session_name}"


LAST_LINES = 2000


WINDOW = re.compile(r"@[0-9]+")


PER_COLUMN = 3


GRID_MANAGER = "@grid-manager"


GRID_PER_COLUMN = "@grid-per-column"


GRID_TILES = "@grid-tiles"


GRID_BUSY = "@grid-busy"


GRID_PANES_SET = "@grid-panes"  # sorted pane ids at the last tile


PANE_SET = re.compile(r"%[0-9]+( %[0-9]+)*")


GRID_HOOKS = ("pane-exited", "window-resized", "window-layout-changed")


RETILES = ("all", "open-close", "off")


RETILE = "open-close"


RETILE_HOOKS = {
    "all": GRID_HOOKS,
    "open-close": ("pane-exited", "window-layout-changed"),
    "off": (),
}


GRID_WINDOW = (
    "#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{window_layout}\t"
    f"#{{{GRID_MANAGER}}}\t#{{{GRID_PER_COLUMN}}}\t#{{{GRID_PANES_SET}}}\t#{{{GRID_TILES}}}"
)


GRID_PANES = (
    f"#{{window_id}}\t#{{"
    f"{GRID_MANAGER}"
    f"}}\t#{{pane_id}}\t#{{pane_width}}\t#{{pane_height}}\t#{{socket_path}}"
)


GRID_ROW = re.compile(r"(@[0-9]+)\t([^\t]*)\t(%[0-9]+)\t([0-9]+)\t([0-9]+)\t([^\t]+)")


HOOK_PATH = re.compile(r"[A-Za-z0-9_./@+-]+")


TILE_WAIT = 5


TILE_POLL = 0.05


TILE_LIMIT = 50  # re-tiles per TILE_WINDOW s; one more stops auto re-tiling


TILE_WINDOW = 10


SETTLE_ITERATIONS = 3


LEAF, LEFT_RIGHT, TOP_BOTTOM = "", "{}", "[]"


LAYOUT_CELL = re.compile(r"([0-9]+)x([0-9]+),([0-9]+),([0-9]+)(?:,([0-9]+)|([{[]))")


PGREP = ["pgrep", "-a", "-x", "iTerm2"]


APPLESCRIPT = (
    "on run argv\n  set {splitDir, anchorKind, tmuxPath, sessionName} to "
    "items 1 thru 4 of argv\n  set paneCommand to (quoted form of tmuxPath) "
    '& " attach -t " & (quoted form of ("=" & sessionName))\n  if '
    'application "iTerm2" is running then\n    tell application "iTerm2"\n   '
    "   repeat with i from 5 to count of argv\n        set anchorValue to "
    "item i of argv\n        repeat with w in windows\n          repeat with "
    "t in tabs of w\n            repeat with s in sessions of t\n            "
    '  if (anchorKind is "id" and unique id of s is anchorValue) or '
    '(anchorKind is "tty" and tty of s is anchorValue) then\n                '
    'if splitDir is "right" then\n                  tell s to set newSession '
    "to split vertically with default profile command paneCommand\n          "
    "      else\n                  tell s to set newSession to split "
    "horizontally with default profile command paneCommand\n                "
    'end if\n                return "ok " & (unique id of newSession)\n      '
    "        end if\n            end repeat\n          end repeat\n        "
    'end repeat\n      end repeat\n    end tell\n    return "no iTerm2 pane '
    'shows the anchor"\n  end if\n  return "iTerm2 is not running"\nend '
    "run"
)


TERMINAL_KEYS = (
    "TMUX",
    "TMUX_PANE",
    "TERM",
    "COLORTERM",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "TERM_SESSION_ID",
    "ITERM_SESSION_ID",
    "ITERM_PROFILE",
    "LC_TERMINAL",
    "LC_TERMINAL_VERSION",
    "COLUMNS",
    "LINES",
)


EXEC = f"""import errno, json, os, signal, sys
with open(sys.argv[1]) as f: h = json.load(f)
signal.pthread_sigmask(signal.SIG_BLOCK, h.get('block', []))
w = os.open(sys.argv[2], os.O_WRONLY | os.O_NONBLOCK)
try:
    os.chdir(h['cwd'])
    os.unlink(sys.argv[1])
    h['env'].update((k, os.environ[k]) for k in {TERMINAL_KEYS!r} if k in os.environ)
    for s in (signal.SIGPIPE, signal.SIGXFSZ): signal.signal(s, signal.SIG_DFL)
    os.execve(h['argv'][0], h['argv'], h['env'])
except BaseException as e:
    os.write(w, b'%d' % (isinstance(e, OSError) and e.errno or errno.EINVAL))
    raise"""


class Anchor(NamedTuple):
    """The pane a show goes beside: iterm (("tty", ttys) or ("id", [unique id]); None in tmux), else
    `pane` on `socket`.
    """

    iterm: tuple[str, list[str]] | None
    pane: str | None = None
    socket: str | None = None
    session: str | None = None


class Cell(NamedTuple):
    """A layout cell: w x h at x, y; a LEAF shows pane, a container holds children."""

    w: int
    h: int
    x: int
    y: int
    pane: str | None = None
    kind: str = LEAF
    children: tuple[Cell, ...] = ()


class Slot(NamedTuple):
    """A grid cell: a pane and the slots stacked right of it, or (pane None) an orphan group."""

    pane: str | None
    children: tuple[Slot, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Command:
    argv: Sequence[str]
    cwd: str
    env: Mapping[str, str]


@dataclass(frozen=True, kw_only=True)
class StartOptions:
    events: str | None = None
    status_line: bool = False
    tags: Mapping[str, str] | None = None


@dataclass(frozen=True, kw_only=True)
class DetachOptions:
    block: tuple[int, ...] = ()
    opener: str | None = None


@dataclass(frozen=True, kw_only=True)
class PaneOptions:
    split: str | None = None
    split_from: str | None = None
    opener: str | None = None
    per_column: int | None = None
    retile: str | None = None


@dataclass(frozen=True, kw_only=True)
class PaneServices:
    script: str
    proc: Callable[..., subprocess.CompletedProcess] = subprocess.run
    tile_window: Callable[..., None] | None = None


@dataclass(frozen=True, kw_only=True)
class GridSettings:
    per_column: int
    retile: str


@dataclass(frozen=True, kw_only=True)
class GridGeometry:
    width: int
    height: int
    manager_width: int
    per_column: int


@dataclass(frozen=True)
class Rectangle:
    w: int
    h: int
    x: int
    y: int


@dataclass(frozen=True, kw_only=True)
class GridFrame:
    manager: str
    panes: list[str]
    layout: Cell
