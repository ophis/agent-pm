"""Bundled tmux layout operations, independent of the standalone facade."""

from __future__ import annotations

import os
import re
import runpy
from typing import TYPE_CHECKING

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux.model.types import (
        LAYOUT_CELL,
        LEAF,
        LEFT_RIGHT,
        TOP_BOTTOM,
        Cell,
        GridFrame,
        GridGeometry,
        Rectangle,
        Slot,
    )
else:
    _load_file = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_file"]
    _model_types = _load_file(
        "agent_pm.session.hosts.tmux.model.types", os.path.join(DIRECTORY, "model/types.py")
    )
    Cell = _model_types.Cell
    GridFrame = _model_types.GridFrame
    GridGeometry = _model_types.GridGeometry
    LAYOUT_CELL = _model_types.LAYOUT_CELL
    LEAF = _model_types.LEAF
    LEFT_RIGHT = _model_types.LEFT_RIGHT
    Rectangle = _model_types.Rectangle
    Slot = _model_types.Slot
    TOP_BOTTOM = _model_types.TOP_BOTTOM


def layout_checksum(body: str) -> str:
    csum = 0
    for byte in body.encode():
        csum = ((csum >> 1) + ((csum & 1) << 15) + byte) & 0xFFFF
    return f"{csum:04x}"


def parse_layout(text: str) -> Cell | None:
    """The cell tree of `<checksum>,<body>` or a body; None when malformed."""
    csum, sep, body = text.partition(",")
    if sep and re.fullmatch(r"[0-9a-f]{4}", csum):
        if layout_checksum(body) != csum:
            return None
        text = body
    try:
        cell, end = parse_cell(text, 0)
    except ValueError:
        return None
    return cell if end == len(text) else None


def parse_cell(text: str, at: int) -> tuple[Cell, int]:
    m = LAYOUT_CELL.match(text, at)
    if m is None:
        raise ValueError(at)
    w, h, x, y = (int(v) for v in m.group(1, 2, 3, 4))
    if m.group(5) is not None:
        return Cell(w, h, x, y, f"%{m.group(5)}"), m.end()
    kind = LEFT_RIGHT if m.group(6) == "{" else TOP_BOTTOM
    at, children = m.end(), []
    while True:
        child, at = parse_cell(text, at)
        children.append(child)
        if text.startswith(kind[1], at):
            return Cell(w, h, x, y, kind=kind, children=tuple(children)), at + 1
        if not text.startswith(",", at):
            raise ValueError(at)
        at += 1


def render_layout(cell: Cell) -> str:
    body = layout_body(cell)
    return f"{layout_checksum(body)},{body}"


def layout_body(c: Cell) -> str:
    head = f"{c.w}x{c.h},{c.x},{c.y}"
    if c.kind == LEAF:
        return f"{head},{leaf_pane(c)[1:]}"
    return head + c.kind[0] + ",".join(layout_body(child) for child in c.children) + c.kind[1]


def leaf_pane(cell: Cell) -> str:
    assert cell.pane is not None
    return cell.pane


def layout_leaves(cell: Cell) -> list[Cell]:
    """Depth first: select-layout's pane order."""
    return (
        [cell]
        if cell.kind == LEAF
        else [leaf for child in cell.children for leaf in layout_leaves(child)]
    )


def layout_paths(cell: Cell, above: tuple[Cell, ...] = ()) -> dict[str, list[Cell]]:
    if cell.kind == LEAF:
        return {leaf_pane(cell): [*above, cell]}
    return {
        pane: path
        for child in cell.children
        for pane, path in layout_paths(child, (*above, cell)).items()
    }


def natural_order(name: str) -> list[str | int]:
    """Text and number chunks: w2 before w10."""
    return [int(c) if i % 2 else c for i, c in enumerate(re.split(r"([0-9]+)", name))]


def grid_tree(
    manager: str, panes: list[str], sessions: list[tuple[int, str, str, str]], layout: Cell
) -> list[Slot]:
    """The main grid's cells in order: a session's node sits in its @opener's, an orphan group (its
    @opener gone) where the layout shows it, the rest in main; main by _natural name, siblings by
    pane id, manual panes last.
    """
    owner = {}
    for _, name, opener, pane in sorted(sessions):
        if pane in panes and pane != manager and pane not in owner:
            owner[pane] = name, opener
    node = {name: pane for pane, (name, _) in owner.items()}
    names = {row[1] for row in sessions}
    parent = {}  # a node's pane, or an orphan group's name -> its parent's; None: main
    for pane, (_, opener) in owner.items():
        parent[pane] = node.get(opener, opener if opener and opener not in names else None)

    paths = layout_paths(layout)
    rank = {pane: i for i, pane in enumerate(paths)}
    spans = adopt_orphans(
        parent, owner, paths, GridFrame(manager=manager, panes=panes, layout=layout)
    )
    reached, todo = set(), [None]
    while todo:
        key = todo.pop()
        found = [k for k, up in parent.items() if up == key and k not in reached]
        reached.update(found)
        todo += found
    parent.update((k, None) for k in list(parent) if k not in reached and parent[k] not in spans)

    manual = sorted((p for p in panes if p != manager and p not in owner), key=lambda p: int(p[1:]))
    return [*family_slots(parent, owner, rank), *(Slot(p) for p in manual)]


def grid_layout(cells: list[Slot], manager: str, *, geometry: GridGeometry) -> Cell | None:
    """The manager left at manager_width (narrowed to fit), cells in columns of per_column; None: it
    does not fit.
    """
    width, height = geometry.width, geometry.height
    manager_width, per_column = geometry.manager_width, geometry.per_column
    columns = tuple(
        Slot(None, tuple(cells[i : i + per_column])) for i in range(0, len(cells), per_column)
    )
    c = len(columns)
    mw = min(
        manager_width if manager_width != width else (width - 1) // 2,
        width - 1 - (c * max(minimum_width(s) for s in cells) + c - 1),
    )
    if mw < 1:
        return None
    root = Cell(
        width,
        height,
        0,
        0,
        kind=LEFT_RIGHT,
        children=(
            Cell(mw, height, 0, 0, manager),
            layout_slots(columns, LEFT_RIGHT, Rectangle(width - mw - 1, height, mw + 1, 0)),
        ),
    )
    return root if all(leaf.h >= 1 for leaf in layout_leaves(root)) else None


def minimum_width(s: Slot) -> int:
    if not s.children:
        return 1
    least = max(minimum_width(child) for child in s.children)
    return least if s.pane is None else 2 * least + 1


def place_slot(s: Slot, rect: Rectangle) -> Cell:
    """A pane takes the left half, its children stack right; an orphan group stacks."""
    w, h, x, y = rect.w, rect.h, rect.x, rect.y
    if not s.children:
        return Cell(w, h, x, y, s.pane)
    if s.pane is None:
        return layout_slots(s.children, TOP_BOTTOM, rect)
    left = w // 2
    return Cell(
        w,
        h,
        x,
        y,
        kind=LEFT_RIGHT,
        children=(
            Cell(left, h, x, y, s.pane),
            layout_slots(s.children, TOP_BOTTOM, Rectangle(w - left - 1, h, x + left + 1, y)),
        ),
    )


def layout_slots(slots: tuple[Slot, ...], kind: str, rect: Rectangle) -> Cell:
    """Side by side or stacked, equal sizes, the remainder to the first."""
    w, h, x, y = rect.w, rect.h, rect.x, rect.y
    if len(slots) == 1:
        return place_slot(slots[0], rect)
    across = kind == LEFT_RIGHT
    size, extra = divmod((w if across else h) - len(slots) + 1, len(slots))
    children, at = [], x if across else y
    for i, s in enumerate(slots):
        n = size + (i < extra)
        children.append(
            place_slot(s, Rectangle(n, h, at, y))
            if across
            else place_slot(s, Rectangle(w, n, x, at))
        )
        at += n + 1
    return Cell(w, h, x, y, kind=kind, children=tuple(children))


def pane_swaps(have: list[str], want: list[str]) -> list[tuple[str, str]]:
    """The fewest swap-pane pairs turning index order have into want."""
    have, swaps = list(have), []
    for i, pane in enumerate(want):
        if have[i] != pane:
            j = have.index(pane)
            swaps.append((pane, have[i]))
            have[i], have[j] = pane, have[i]
    return swaps


def descendants(key, owner, parent) -> list[str]:
    return [
        *([key] if key in owner else []),
        *(p for k, up in parent.items() if up == key for p in descendants(k, owner, parent)),
    ]


def adopt_orphans(parent, owner, paths, frame: GridFrame):
    manager, panes, layout = frame.manager, frame.panes, frame.layout
    columns = (
        layout.children[1]
        if layout.kind == LEFT_RIGHT
        and len(layout.children) == 2
        and layout.children[0].pane == manager
        else layout
    )
    groups = sorted({up for up in parent.values() if up is not None and up not in owner})
    spans = {q: set(descendants(q, owner, parent)) for q in groups}
    mains = [p for p in panes if p != manager and parent.get(p) is None]
    for q in groups:
        path = paths[min(spans[q])]
        for pane in spans[q]:
            path = [a for a, b in zip(path, paths[pane], strict=False) if a is b]
        if path[-1] is layout or path[-1] is columns:
            parent.update((k, None) for k, up in list(parent.items()) if up == q)
            continue
        parent[q] = None
        for cell in reversed(path):
            head = cell.children[0] if cell.kind == LEFT_RIGHT else None
            if head is None or head.kind != LEAF or head.pane in spans[q]:
                continue
            # R is a pane's own cell ({pane, its children}) only with two children and that pane the
            # one main pane
            if (
                head.pane == manager
                or cell is columns
                and (len(cell.children) != 2 or mains != [head.pane])
            ):
                break
            if head.pane in owner:
                parent[q] = head.pane
                break
    return spans


def family_slots(parent, owner, rank):
    def first(key) -> int:
        return min(rank[p] for p in descendants(key, owner, parent))

    def slots(key) -> list[Slot]:
        if key is None:
            order = sorted(
                (k for k, up in parent.items() if up is None),
                key=lambda k: natural_order(owner[k][0] if k in owner else k),
            )
        else:
            live = sorted(
                (k for k, up in parent.items() if up == key and k in owner),
                key=lambda p: int(p[1:]),
            )
            order = list(live)
            for q in sorted(
                (k for k, up in parent.items() if up == key and k not in owner), key=first
            ):
                later = next((k for k in live if first(k) > first(q)), None)
                order.insert(len(order) if later is None else order.index(later), q)
        return [Slot(k if k in owner else None, tuple(slots(k))) for k in order]

    return slots(None)
