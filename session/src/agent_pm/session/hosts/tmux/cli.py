"""The tmux grid hooks' command: tile (entry.py runs it from the hooks' import roots). Everything
else a session command does is agent-pm-session's (session/cli.py).
"""

from __future__ import annotations

import argparse
import sys

from agent_pm.session.hosts.tmux.grid import tile
from agent_pm.session.hosts.tmux.types import TuiError


def main(argv: list[str] | None = None, *, operations=None) -> int:
    actions = {"tile": tile, **(operations or {})}
    ap = argparse.ArgumentParser(
        prog="tmux grid",
        description="Lay out a grid window's panes; its tmux hooks run this. By hand: "
        "agent-pm-session tile.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
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
    a = ap.parse_args(sys.argv[1:] if argv is None else list(argv))
    try:
        actions["tile"](a.window, wait=not a.no_wait, if_panes_changed=a.if_panes_changed)
    except TuiError as e:
        print(f"tui: {e}", file=sys.stderr)
        return 1
    return 0
