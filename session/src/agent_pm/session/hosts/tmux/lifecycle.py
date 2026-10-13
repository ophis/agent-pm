"""Bundled tmux lifecycle operations, independent of the standalone facade."""

from __future__ import annotations

import contextlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux.commands import (
        attach_command,
        call_tmux,
        events_file,
        kill,
        session_decorations,
        session_name,
        status,
        tmux_error,
    )
    from agent_pm.session.hosts.tmux.errors.types import (
        TuiError,
    )
    from agent_pm.session.hosts.tmux.model.types import (
        ERRPIPE,
        EXEC,
        HANDOVER_TIMEOUT,
        ITERM_OPENER,
        OPENER,
        POLL,
        RUNNING,
        TERMINAL_KEYS,
        USER_OPTION,
        Command,
        DetachOptions,
        StartOptions,
    )
else:
    _load_file = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_file"]
    _commands = _load_file(
        "agent_pm.session.hosts.tmux.commands", os.path.join(DIRECTORY, "commands.py")
    )
    attach_command = _commands.attach_command
    call_tmux = _commands.call_tmux
    events_file = _commands.events_file
    kill = _commands.kill
    session_decorations = _commands.session_decorations
    session_name = _commands.session_name
    status = _commands.status
    tmux_error = _commands.tmux_error
    _errors_types = _load_file(
        "agent_pm.session.hosts.tmux.errors.types", os.path.join(DIRECTORY, "errors/types.py")
    )
    TuiError = _errors_types.TuiError
    _model_types = _load_file(
        "agent_pm.session.hosts.tmux.model.types", os.path.join(DIRECTORY, "model/types.py")
    )
    Command = _model_types.Command
    DetachOptions = _model_types.DetachOptions
    ERRPIPE = _model_types.ERRPIPE
    EXEC = _model_types.EXEC
    HANDOVER_TIMEOUT = _model_types.HANDOVER_TIMEOUT
    ITERM_OPENER = _model_types.ITERM_OPENER
    OPENER = _model_types.OPENER
    POLL = _model_types.POLL
    RUNNING = _model_types.RUNNING
    StartOptions = _model_types.StartOptions
    TERMINAL_KEYS = _model_types.TERMINAL_KEYS
    USER_OPTION = _model_types.USER_OPTION


def start(
    session: str,
    command: Command,
    *,
    options: StartOptions | None = None,
    proc=subprocess.run,
    sleep=time.sleep,
) -> None:
    """Runs argv in a new detached session that keeps its pane once argv ends, in cwd with env (its
    terminal keys the pane's, PWD cwd), tags, @events, the pane-died hook (@state dead, a dead line
    to @events), the `<session> <state>` border and the status line set in the same tmux call (an
    instant death still gets its dead line); opens no pane. A running session is refused, a dead one
    replaced; argv reaches the pane through a 0600 handover file. Returns once argv has exec'd;
    raising, it leaves no session of its own.
    """
    settings = StartOptions() if options is None else options
    argv, cwd, env = command.argv, command.cwd, command.env
    events, status_line, tags = settings.events, settings.status_line, settings.tags
    session_name(session)
    tag_options = session_tags(session, tags or {})
    argv = prepare_command(argv, env)
    if events is not None:
        events = events_file(events)
    tmp = pipe = None
    may_run = False
    try:
        tmp = tempfile.mkdtemp()
        path, wrapper = write_wrapper(tmp)
        state = status(session, proc=proc)
        if state == RUNNING:
            raise TuiError.session_running(session, attach_command(session))
        if isinstance(state, int):
            kill(session, proc=proc)
        pipe = handover(path, argv, cwd, env)
        cols, rows = shutil.get_terminal_size()
        decorations = [
            arg
            for args in session_decorations(session, events, status_line=status_line)
            for arg in (";", *args)
        ]
        # new-session may have run even when the tmux call fails
        may_run = True
        res = call_tmux(
            [
                "new-session",
                "-d",
                "-s",
                session,
                "-x",
                str(cols),
                "-y",
                str(rows),
                *wrapper,
                ";",
                "set-option",
                "-p",
                "-t",
                f"={session}:",
                "remain-on-exit",
                "on",
                *tag_options,
                *decorations,
            ],
            proc,
        )
        if res.returncode:
            if tmux_error(res).startswith(
                "duplicate session:"
            ):  # another start took the name: not ours to kill
                may_run = False
            raise TuiError.command_failed(tmux_error(res))
        if not wait_started(path, pipe, argv[0], sleep):
            raise TuiError.session_not_started()
        may_run = False
    except OSError as e:
        raise TuiError.handover_failed(e) from e
    finally:
        cleanup_launch(tmp, pipe, session, proc, may_run=may_run)


def detach(
    session: str,
    command: Command,
    *,
    options: DetachOptions | None = None,
    proc=subprocess.run,
    sleep=time.sleep,
) -> None:
    """Runs argv as start does, `block` blocked until argv unblocks them; env's full iTerm2
    $ITERM_SESSION_ID stays and opener becomes @opener. No decoration, no kept pane. An existing
    session is refused untouched.
    """
    settings = DetachOptions() if options is None else options
    argv, cwd, env = command.argv, command.cwd, command.env
    block, opener = settings.block, settings.opener
    session_name(session)
    if opener is not None and not OPENER.fullmatch(opener):
        raise TuiError.invalid_opener(opener)
    validate_detach_arguments(argv)
    argv = prepare_command(argv, env)
    iterm = env.get("ITERM_SESSION_ID", "")
    terminal = ["-e", f"ITERM_SESSION_ID={iterm}"] if ITERM_OPENER.fullmatch(iterm) else []
    tmp = pipe = None
    may_run = False
    try:
        tmp = tempfile.mkdtemp()
        path, wrapper = write_wrapper(tmp)
        pipe = handover(path, argv, cwd, env, block=[int(s) for s in block])
        may_run = True
        res = call_tmux(
            [
                "new-session",
                "-d",
                *terminal,
                "-s",
                session,
                *wrapper,
                *(
                    [";", "set-option", "-t", f"={session}:", "@opener", opener]
                    if opener is not None
                    else []
                ),
            ],
            proc,
        )
        if res.returncode:
            if tmux_error(res).startswith("duplicate session:"):
                may_run = False
                raise TuiError.existing_session(session)
            raise TuiError.command_failed(tmux_error(res))
        if not wait_started(path, pipe, argv[0], sleep):
            raise TuiError.session_not_started(session)
        may_run = False
    except OSError as e:
        raise TuiError.handover_failed(e) from e
    finally:
        cleanup_launch(tmp, pipe, session, proc, may_run=may_run)


def prepare_command(argv: Sequence[str], env: Mapping[str, str]) -> list[str]:
    if not argv:
        raise TuiError.empty_command()
    exe = shutil.which(argv[0], path=env.get("PATH", os.defpath))
    if exe is None:
        raise TuiError.missing_command(argv[0])
    return [os.path.abspath(exe), *argv[1:]]


def write_wrapper(tmp: str) -> tuple[str, list[str]]:
    path, fifo = os.path.join(tmp, "handover.json"), os.path.join(tmp, ERRPIPE)
    for arg in (sys.executable, path, fifo):
        if "#" in arg or arg.endswith(";"):
            raise TuiError.misread_argument(arg)
    return path, [sys.executable, "-I", "-c", EXEC, path, fifo]


def handover(
    path: str,
    argv: list[str],
    cwd: str,
    env: Mapping[str, str],
    block: list[int] | None = None,
) -> int:
    cwd = os.path.abspath(cwd)
    child = {k: v for k, v in env.items() if k not in TERMINAL_KEYS}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(
            {
                "argv": argv,
                "cwd": cwd,
                "env": {**child, "PWD": cwd},
                **({"block": block} if block is not None else {}),
            },
            f,
        )
    fifo = os.path.join(os.path.dirname(path), ERRPIPE)
    os.mkfifo(fifo, 0o600)
    return os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)


def session_tags(session: str, tags: Mapping[str, str]) -> list[str]:
    args = []
    for key, value in tags.items():
        if not USER_OPTION.fullmatch(key) or not isinstance(value, str) or value.endswith(";"):
            raise TuiError.invalid_tag(key, value, USER_OPTION.pattern)
        args += [";", "set-option", "-t", f"={session}:", key, value]
    return args


def wait_started(path: str, pipe: int, command: str, sleep) -> bool:
    """
    Whether the handover went, then execve closed the errpipe, each in time; errpipe data raises.
    """
    polls, out = 0, b""
    while os.path.exists(path):
        if polls == round(HANDOVER_TIMEOUT / POLL):
            return False
        sleep(POLL)
        polls += 1
    polls = 0
    while True:  # only now: a FIFO no writer opened reads EOF
        try:
            chunk = os.read(pipe, 64)
        except BlockingIOError:
            if polls >= round(HANDOVER_TIMEOUT / POLL):
                return False
            sleep(POLL)
            polls += 1
            continue
        if not chunk:
            break
        out += chunk
    if out:
        try:
            reason = os.strerror(int(out))
        except (ValueError, OverflowError):
            reason = "bad errpipe data"
        raise TuiError.failed_exec(command, reason)
    return True


def validate_detach_arguments(argv):
    for arg in argv:
        if "\0" in arg:
            raise TuiError.nul_argument()
        try:
            os.fsencode(arg)
        except UnicodeEncodeError:
            raise TuiError.unencodable_argument() from None


def cleanup_launch(tmp, pipe, session, proc, *, may_run: bool):
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)  # first, so a late wrapper finds no file to run
    if pipe is not None:
        os.close(pipe)
    if may_run:
        with contextlib.suppress(TuiError):
            kill(session, proc=proc)
