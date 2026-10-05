"""Attended runs: where the TUI pane goes, and the harness-only record of a run's TUI sessions.

The record, logs/tui/<ID>, holds one TUI session name per line. Runs never write logs/; the inner driver does.
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
import tui  # noqa: E402
from linear import one_line  # noqa: E402

RECORD_DIR = "tui"


class Bad(Exception):
    """No usable place for the TUI pane; the message is for the user."""


def layout(split, beside, *, proc=subprocess.run, isatty=os.isatty):
    """(drive.Layout, attach): attach is True when the outer must attach this terminal to the driver session."""
    if split is not None and split not in tui.SPLITS:
        raise Bad(f"split must be one of {', '.join(tui.SPLITS)}")
    split = split or drive.Layout.split
    if beside is not None:
        if not tui.NAME.fullmatch(beside):
            raise Bad("bad tmux session name")
        if tui.status(beside, proc=proc) is None:
            raise Bad(f"no tmux session {beside}")
        return drive.Layout(split, beside), False
    if os.environ.get("TMUX"):
        try:
            return drive.Layout(split, tui.own_session(proc=proc)), False
        except tui.TuiError as e:
            raise Bad(one_line(e)) from e
    if isatty(0):
        return drive.Layout(split, None), True
    raise Bad("no pane to show the TUI beside: run from tmux or a terminal, or pass --beside SESSION")


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
                if tui.status(name, proc=proc) is None:
                    continue
                tui.kill(name, proc=proc)
                out.append(Closed("closed", name))
            except tui.TuiError as e:
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
