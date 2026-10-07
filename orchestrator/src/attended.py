"""Attended runs: where the TUI pane goes, and the name of an agent run's TUI session, <role>-<ID>-<sid[:8]>.

attended.py owns that name: prefix builds it, ISSUE_TUI matches it. An issue's TUI sessions are the live tmux sessions
so named; nothing records them.
"""
import os
import re
import subprocess
import sys
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402,F401  (puts core/src on sys.path)
import drive  # noqa: E402
import linear  # noqa: E402
import tui_claude  # noqa: E402
from linear import one_line  # noqa: E402

# the prefix of an issue's TUI session name (drive.TUI_SESSION): <role>-<ID>
ISSUE_TUI = re.compile(rf"([a-z][a-z0-9-]*)-({linear.ISSUE_ID})")
# list-sessions' stderr when no server runs: a stale socket, or none (tmux's client.c)
NO_SERVER = re.compile(r"no server running on .*|error connecting to .* \(No such file or directory\)")


class Bad(Exception):
    """No usable place for the TUI pane; the message is for the user."""


def layout(split, anchor, *, proc=subprocess.run):
    """The drive.Layout for the TUI pane: split and anchor as given, and the caller's opener (tui_claude.opener). Bad
    unless tui_claude.anchor finds a pane (anchor's, else the caller's), and, without anchor, the caller has an
    opener."""
    if split is not None and split not in tui_claude.SPLITS:
        raise Bad(f"split must be one of {', '.join(tui_claude.SPLITS)}")
    if anchor is not None:
        if not tui_claude.NAME.fullmatch(anchor):
            raise Bad("bad tmux session name")
        if tui_claude.status(anchor, proc=proc) is None:
            raise Bad(f"no tmux session {anchor}")
    try:
        tui_claude.anchor(anchor, proc=proc)
        try:
            opener = tui_claude.opener(proc=proc)
        except tui_claude.TuiError:
            if anchor is None:
                raise
            opener = None
    except tui_claude.TuiError as e:
        hint = "" if anchor else ": run from tmux or iTerm2, or pass --anchor SESSION"
        raise Bad(f"no pane to show the TUI beside ({one_line(str(e))}){hint}") from e
    return drive.Layout(split, anchor, opener)


def prefix(role, ident):
    """The TUI session prefix of role's agent run of issue ident, <role>-<ID>; ValueError when ISSUE_TUI does not match
    it."""
    name = f"{role}-{ident}"
    if not ISSUE_TUI.fullmatch(name):
        raise ValueError(f"TUI session prefix {name!r}: want {ISSUE_TUI.pattern}")
    return name


def sessions(ident, *, proc=subprocess.run):
    """The names of issue ident's live TUI sessions, sorted; [] when no tmux server runs. Raises tui_claude.TuiError."""
    return sorted(name for name, i in _live(proc) if i == ident)


def issues(*, proc=subprocess.run):
    """The issue ids with a live TUI session, sorted; [] when no tmux server runs. Raises tui_claude.TuiError."""
    return sorted({i for _, i in _live(proc)})


def _live(proc):
    """(name, issue id) of each live tmux session named as an issue's TUI session."""
    try:
        res = proc(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    except OSError as e:
        raise tui_claude.TuiError(f"tmux: {e}") from e
    if res.returncode:
        err = (res.stderr or "").strip()
        if NO_SERVER.fullmatch(err):
            return []
        raise tui_claude.TuiError(f"tmux: {err}")
    found = []
    for name in res.stdout.splitlines():
        if (m := drive.TUI_SESSION.fullmatch(name)) and (issue := ISSUE_TUI.fullmatch(m["prefix"])):
            found.append((name, issue[2]))
    return found


class Closed(NamedTuple):
    """One close result: status closed or error (name None: the listing failed); msg says why for error."""
    status: str
    name: str | None = None
    msg: str = ""


def close(ident, *, proc=subprocess.run):
    """Kill issue ident's live TUI sessions (sessions). Returns the Closed results; never raises."""
    try:
        names = sessions(ident, proc=proc)
    except tui_claude.TuiError as e:
        return [Closed("error", msg=one_line(e))]
    out = []
    for name in names:
        try:
            tui_claude.kill(name, proc=proc)
            out.append(Closed("closed", name))
        except tui_claude.TuiError as e:
            out.append(Closed("error", name, one_line(e)))
    return out
