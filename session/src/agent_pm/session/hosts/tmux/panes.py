"""Bundled tmux panes operations, independent of the standalone facade."""

from __future__ import annotations

import contextlib
import itertools
import os
import runpy
import shlex
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux import anchors as _anchors
    from agent_pm.session.hosts.tmux import commands as _commands
    from agent_pm.session.hosts.tmux import grids as _grids
    from agent_pm.session.hosts.tmux import validation as _validation
    from agent_pm.session.hosts.tmux.errors import types as _errors
    from agent_pm.session.hosts.tmux.model import types as _model
else:
    load_siblings = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_siblings"]
    _anchors, _commands, _errors, _grids, _model, _validation = load_siblings(
        DIRECTORY,
        ("agent_pm.session.hosts.tmux.anchors", "anchors.py"),
        ("agent_pm.session.hosts.tmux.commands", "commands.py"),
        ("agent_pm.session.hosts.tmux.errors.types", "errors/types.py"),
        ("agent_pm.session.hosts.tmux.grids", "grids.py"),
        ("agent_pm.session.hosts.tmux.model.types", "model/types.py"),
        ("agent_pm.session.hosts.tmux.validation", "validation.py"),
    )


ENTRY_PATH = os.path.join(DIRECTORY, "tmux_host.py")


def show(
    session: str,
    template: str | None = None,
    *,
    options: _model.PaneOptions | None = None,
    services: _model.PaneServices | None = None,
) -> str | None:
    """Prints how to attach, then runs template, else open_pane; "" runs nothing. A failure is
    returned, never raised, but open_pane's TuiError for a bad argument.
    """
    options = _model.PaneOptions() if options is None else options
    services = _model.PaneServices(script=ENTRY_PATH) if services is None else services
    proc = services.proc
    print(f"tui: session {session}: {_commands.attach_command(session)}", file=sys.stderr)
    if template is None:
        why = open_pane(session, options=options, services=services)
    else:
        why = run_template(session, template, proc) if template else None
    if why:
        print(f"tui: show: {why}", file=sys.stderr)
    return why


def open_pane(
    session: str,
    *,
    options: _model.PaneOptions | None = None,
    services: _model.PaneServices | None = None,
) -> str | None:
    """Opens a pane on the session, recording @opener and @pane. Grid mode (_grid): splits the pane
    _grid picks and tiles the window per retile and per_column, ignoring split and split_from. Else
    below the newest open @pane of the opener's other sessions, else right of the opener's pane; or
    beside split_from's pane on side split. None or the failure.
    """
    options = _model.PaneOptions() if options is None else options
    services = _model.PaneServices(script=ENTRY_PATH) if services is None else services
    split, split_from, opener = options.split, options.split_from, options.opener
    per_column, retile = options.per_column, options.retile
    proc = services.proc
    _validation.validate_layout(split, split_from, opener)
    _validation.validate_per_column(per_column)
    _validation.validate_retile(retile)
    tmux = shutil.which("tmux")
    if tmux is None:
        return "tmux not found"
    tmux = os.path.abspath(tmux)
    own = grid = new = None
    try:
        opener, own, sessions, grid = pane_context(options, proc)
        if grid is not None:
            window, manager, at, side = grid
            if split is not None or split_from is not None:
                print("tui: show: grid: --split/--split-from ignored", file=sys.stderr)
            # muted: an old mode's hook tile could land after a later drag; _grid_up unmutes
            _commands.call_tmux(["set-option", "-w", "-t", window, _model.GRID_BUSY, window], proc)
            try:
                new, why = split_pane(session, side, at, tmux, proc)
            finally:
                if new is None:
                    with contextlib.suppress(_errors.TuiError):
                        _commands.call_tmux(
                            ["set-option", "-u", "-w", "-t", window, _model.GRID_BUSY], proc
                        )
        elif split_from is not None:
            new, why = split_pane(
                session, split or "right", _anchors.anchor(split_from, proc=proc), tmux, proc
            )
        elif split is not None:
            assert opener is not None
            new, why = split_pane(
                session, split, _anchors.opener_pane(opener, proc, own=own), tmux, proc
            )
        else:
            assert opener is not None
            new, why = stacked_pane(session, opener, sessions, tmux, proc) or split_pane(
                session, "right", _anchors.opener_pane(opener, proc, own=own), tmux, proc
            )
    except _errors.TuiError as e:
        return f"{e}; watch it with {_commands.attach_command(session)}"
    if new is None:
        return why
    record_pane(session, opener, new, proc)
    if grid is None:
        return None
    return _grids.open_grid(
        window,
        manager,
        _model.GridSettings(
            per_column=_model.PER_COLUMN if per_column is None else per_column,
            retile=_model.RETILE if retile is None else retile,
        ),
        services,
    )


def stacked_pane(
    session: str, opener: str, sessions: list[list[str]], tmux: str, proc
) -> tuple[str | None, str | None] | None:
    """None when no @pane of the opener's other sessions is open."""
    rows = [
        (int(f[0][1:]), f[3], f[4])
        for f in sessions
        if f[1] != session
        and f[2] == opener
        and (_model.PANE.fullmatch(f[3]) or _model.ITERM_ID.fullmatch(f[3]))
    ]
    live = None
    for is_tmux, group in itertools.groupby(
        sorted(rows, reverse=True), key=lambda r: bool(_model.PANE.fullmatch(r[1]))
    ):
        group = list(group)
        if not is_tmux:  # open iff the AppleScript finds it: one try covers them, newest first
            with contextlib.suppress(_errors.TuiError):
                return split_pane(
                    session, "below", _model.Anchor(("id", [r[1] for r in group])), tmux, proc
                )
            continue
        if live is None:
            res = _commands.call_tmux(["list-panes", "-a", "-F", "#{pane_id}"], proc)
            live = set(res.stdout.split("\n")) if res.returncode == 0 else set()
        for _, pane, socket in group:
            if pane in live:
                return split_pane(session, "below", _model.Anchor(None, pane, socket), tmux, proc)
    return None


def split_pane(
    session: str, side: str, a: _model.Anchor, tmux: str, proc
) -> tuple[str | None, str | None]:
    """(new pane, None) or (None, tmux's failure), in iTerm2 when one shows a's pane; TuiError with
    no pane.
    """
    if a.iterm:
        try:
            return split_iterm(session, side, tmux, a.iterm, proc), None
        except _errors.TuiError:
            if a.pane is None:
                raise
    assert a.pane is not None and a.socket is not None
    command = [
        "env",
        "-u",
        "TMUX",
        tmux,
        "-S",
        a.socket,
        "attach",
        "-t",
        f"={session}",
    ]  # argv: as a sh -c string it died at once (tmux 3.7)
    for arg in command:
        if "#" in arg or arg.endswith(";"):
            raise _errors.TuiError.misread_argument(arg)
    try:
        res = _commands.call_tmux(
            [
                "split-window",
                "-d",
                "-h" if side == "right" else "-v",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                a.pane,
                *command,
            ],
            proc,
        )
    except _errors.TuiError as e:
        return None, str(e)
    return (
        (None, f"tmux: {_commands.tmux_error(res)}")
        if res.returncode
        else (res.stdout.strip(), None)
    )


def split_iterm(session: str, split: str, tmux: str, iterm: tuple[str, list[str]], proc) -> str:
    try:
        running = (
            proc(_model.PGREP, capture_output=True, text=True, stdin=subprocess.DEVNULL).returncode
            == 0
        )
    except OSError:
        running = False
    if not running:
        raise _errors.TuiError.iterm_not_running()
    kind, anchors = iterm
    try:
        res = proc(
            ["osascript", "-e", _model.APPLESCRIPT, split, kind, tmux, session, *anchors],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_model.SHOW_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise _errors.TuiError.osascript_timeout(_model.SHOW_TIMEOUT) from None
    except OSError as e:
        raise _errors.TuiError.osascript_failed(e) from e
    if res.returncode:
        raise _errors.TuiError.osascript_failed(_commands.tmux_error(res))
    out = res.stdout.strip()
    if out.startswith("ok ") and _model.ITERM_ID.fullmatch(out[3:]):
        return out[3:]
    raise _errors.TuiError.osascript_output(out)


def record_pane(session: str, opener: str | None, pane: str, proc) -> None:
    """For the next placement; a failure is only printed."""
    if not (_model.PANE.fullmatch(pane) or _model.ITERM_ID.fullmatch(pane)):
        print(f"tui: show: @pane: not a pane id: {pane!r}", file=sys.stderr)
        return
    for key, value in (("@opener", opener), ("@pane", pane)):
        if value is None:
            continue
        try:
            _commands.checked_tmux(["set-option", "-t", f"={session}:", key, value], proc)
        except _errors.TuiError as e:
            print(f"tui: show: {key}: {e}", file=sys.stderr)
            return


def run_template(session: str, template: str, proc) -> str | None:
    unknown = [
        m.group(0) for m in _model.SLOT.finditer(template) if m.group(1) not in _model.PLACEHOLDERS
    ]
    if unknown:
        return f"unknown placeholder {unknown[0]}"
    try:
        res = proc(
            ["/bin/sh", "-c", _model.SLOT.sub(lambda _match: shlex.quote(session), template)],
            stdin=subprocess.DEVNULL,
            timeout=_model.SHOW_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {_model.SHOW_TIMEOUT} s"
    except OSError as e:
        return str(e)
    return f"exit status {res.returncode}" if res.returncode else None


def pane_context(options: _model.PaneOptions, proc):
    split, split_from, opener = options.split, options.split_from, options.opener
    own = None
    if opener is None:
        try:
            opener, own = _anchors.find_opener(proc), True
        except _errors.TuiError:
            if split_from is None:
                raise
    in_tmux = opener is not None and ":" not in opener
    sessions = (
        _anchors.list_sessions(proc) if in_tmux or (split is None and split_from is None) else []
    )
    grid = (
        _grids.find_grid(opener, sessions, proc, own=own)
        if opener is not None and in_tmux
        else None
    )
    return opener, own, sessions, grid
