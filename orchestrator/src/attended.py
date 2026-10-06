"""Attended runs: where the TUI pane goes, and the harness-only record of an agent run's TUI sessions.

The record, logs/tui/<ID>, holds one TUI session name per line. Agent runs never write logs/; the inner driver does.
"""
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import drive  # noqa: E402
import linear  # noqa: E402
import tui_claude  # noqa: E402
from linear import one_line  # noqa: E402

RECORD_DIR = "tui"


class Bad(Exception):
    """No usable place for the TUI pane; the message is for the user."""


def layout(split, beside, *, proc=subprocess.run):
    """The drive.Layout for the TUI pane: beside the pane tui_claude.anchor finds (session `beside`'s, else the caller's tmux
    session's when a terminal shows it, else $ITERM_SESSION_ID's iTerm2 pane)."""
    if split is not None and split not in tui_claude.SPLITS:
        raise Bad(f"split must be one of {', '.join(tui_claude.SPLITS)}")
    split = split or drive.Layout.split
    if beside is not None:
        if not tui_claude.NAME.fullmatch(beside):
            raise Bad("bad tmux session name")
        if tui_claude.status(beside, proc=proc) is None:
            raise Bad(f"no tmux session {beside}")
    try:
        found = tui_claude.anchor(beside, proc=proc)
    except tui_claude.TuiError as e:
        hint = "" if beside else ": run from tmux or iTerm2, or pass --beside SESSION"
        raise Bad(f"no pane to show the TUI beside ({one_line(str(e))}){hint}") from e
    return drive.Layout(split, found.session)


def _path(ident, logs):
    return Path(logs, RECORD_DIR, ident)


def record(ident, name, *, logs=config.LOGS):
    path = _path(ident, logs)
    path.parent.mkdir(parents=True, exist_ok=True)
    drive.append_line(path, name + "\n")


def _read(ident, logs):
    """(names, malformed): the record's TUI session names and its count of other lines; ([], 0) without one.
    Raises OSError."""
    path = _path(ident, logs)
    lines = path.read_text().splitlines() if path.exists() else []
    found = [n for n in lines if drive.TUI_SESSION.fullmatch(n)]
    return found, len(lines) - len(found)


def names(ident, *, logs=config.LOGS):
    """The TUI session names recorded for ident, a malformed line left out. Raises OSError."""
    return _read(ident, logs)[0]


class Closed(NamedTuple):
    """One close result: status closed, skip (a malformed line, name None: its text is never kept) or error (name None:
    the record itself); msg says why for skip and error."""
    status: str
    name: str | None = None
    msg: str = ""


def close(ident, *, logs=config.LOGS, proc=subprocess.run):
    """Kill the live TUI sessions recorded for ident and drop them from the record; a kill that fails stays recorded,
    a session already gone is dropped with no result. Returns the Closed results; never raises."""
    path = _path(ident, logs)
    out, kept = [], []
    try:
        if not path.exists():
            return out
        found, malformed = _read(ident, logs)
        out += [Closed("skip", msg="not a TUI session name")] * malformed
        for name in found:
            try:
                if tui_claude.status(name, proc=proc) is None:
                    continue
                tui_claude.kill(name, proc=proc)
                out.append(Closed("closed", name))
            except tui_claude.TuiError as e:
                kept.append(name)
                out.append(Closed("error", name, one_line(e)))
        if kept:
            drive.save(path, "".join(n + "\n" for n in kept))
        else:
            path.unlink()
    except OSError as e:
        out.append(Closed("error", msg=one_line(e)))
    return out


def recorded(logs=config.LOGS):
    """Issue ids with a record, sorted."""
    try:
        files = os.listdir(Path(logs, RECORD_DIR))
    except OSError:
        return []
    return sorted(n for n in files if re.fullmatch(linear.ISSUE_ID, n))
