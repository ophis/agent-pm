"""TuiError failures raised by the bundled session runtime helpers."""

from __future__ import annotations

import argparse


class TuiError(Exception):
    @classmethod
    def session_running(cls, session: str, attach: str) -> TuiError:
        return cls(f"session {session} is running; {attach}")

    @classmethod
    def command_failed(cls, cause: object) -> TuiError:
        return cls(f"tmux: {cause}")

    @classmethod
    def session_not_started(cls, session: str | None = None) -> TuiError:
        return cls(
            "the session did not start" if session is None else f"session {session} did not start"
        )

    @classmethod
    def handover_failed(cls, cause: Exception) -> TuiError:
        return cls(f"handover: {cause}")

    @classmethod
    def invalid_opener(cls, opener: object) -> TuiError:
        return cls(
            f"invalid opener {opener!r}: want a tmux session name or $ITERM_SESSION_ID's value"
        )

    @classmethod
    def nul_argument(cls) -> TuiError:
        return cls("an argv item holds a NUL character")

    @classmethod
    def unencodable_argument(cls) -> TuiError:
        return cls("an argv item cannot be encoded")

    @classmethod
    def existing_session(cls, session: str) -> TuiError:
        return cls(f"session {session} exists")

    @classmethod
    def empty_command(cls) -> TuiError:
        return cls("no command")

    @classmethod
    def missing_command(cls, command: str) -> TuiError:
        return cls(f"command not found: {command}")

    @classmethod
    def misread_argument(cls, argument: str) -> TuiError:
        return cls(f"tmux would misread {argument!r}")

    @classmethod
    def invalid_tag(cls, key: str, value: object, pattern: str) -> TuiError:
        return cls(f"tag {key!r}={value!r}: want {pattern} and a str tmux takes verbatim")

    @classmethod
    def failed_exec(cls, command: str, reason: str) -> TuiError:
        return cls(f"cannot run {command}: {reason}")

    @classmethod
    def invalid_pane(cls, pane: object) -> TuiError:
        return cls(f"bad tmux pane {pane!r}")

    @classmethod
    def invalid_shown_session(cls, session: object) -> TuiError:
        return cls(f"tmux session {session!r}: want [A-Za-z0-9_-]+")

    @classmethod
    def unsafe_events(cls, path: str) -> TuiError:
        return cls(f"events file {path}: not a regular file owned by you")

    @classmethod
    def events_access(cls, path: str, cause: OSError) -> TuiError:
        return cls(f"events file {path}: {cause.strerror}")

    @classmethod
    def mark_timeout(cls, seconds: float) -> TuiError:
        return cls(f"tmux: timed out after {seconds} s")

    @classmethod
    def misread_events(cls, path: str) -> TuiError:
        return cls(f"events file {path}: tmux would misread it")

    @classmethod
    def invalid_option(cls, name: object, pattern: str) -> TuiError:
        return cls(f"option {name!r}: want {pattern}")

    @classmethod
    def invalid_lines(cls, lines: object) -> TuiError:
        return cls(f"lines must be a positive int, not {lines!r}")

    @classmethod
    def iterm_not_running(cls) -> TuiError:
        return cls("iTerm2 is not running")

    @classmethod
    def osascript_timeout(cls, seconds: float) -> TuiError:
        return cls(f"osascript timed out after {seconds} s")

    @classmethod
    def osascript_failed(cls, cause: object) -> TuiError:
        return cls(f"osascript: {cause}")

    @classmethod
    def osascript_output(cls, output: str) -> TuiError:
        return cls(output or "osascript printed nothing")

    @classmethod
    def invalid_window(cls, window: object, pattern: str) -> TuiError:
        return cls(f"invalid window id {window!r}: want {pattern}")

    @classmethod
    def invalid_grid_socket(cls, window: str, socket: str) -> TuiError:
        return cls(f"grid {window}: socket path {socket!r}: want an absolute path")

    @classmethod
    def grid_lock_failed(cls, path: str, cause: OSError) -> TuiError:
        return cls(f"grid lock {path}: {cause.strerror}")

    @classmethod
    def busy_grid(cls, window: str) -> TuiError:
        return cls(f"grid {window}: busy")

    @classmethod
    def absent_session(cls, session: str | None) -> TuiError:
        return cls(f"no session {session}")

    @classmethod
    def invalid_split(cls, split: object, choices: tuple[str, ...]) -> TuiError:
        return cls(f"split must be one of {', '.join(choices)}, not {split!r}")

    @classmethod
    def invalid_per_column(cls, per_column: object) -> TuiError:
        return cls(f"per_column must be an int from 1 to 9999, not {per_column!r}")

    @classmethod
    def invalid_retile(cls, retile: object, choices: tuple[str, ...]) -> TuiError:
        return cls(f"retile must be one of {', '.join(choices)}, not {retile!r}")

    @classmethod
    def invalid_own_pane(cls, pane: object) -> TuiError:
        return cls(f"bad $TMUX_PANE {pane!r}")

    @classmethod
    def invalid_own_session(cls, name: object) -> TuiError:
        return cls(f"own tmux session {name!r}: want [A-Za-z0-9_-]+")

    @classmethod
    def no_anchor(cls, reason: str | None = None) -> TuiError:
        return cls(
            f"no anchor pane: {'not in tmux' if reason is None else reason}, "
            "no iTerm2 pane ($ITERM_SESSION_ID)"
        )

    @classmethod
    def invalid_iterm_session(cls, value: object, pattern: str) -> TuiError:
        return cls(f"bad $ITERM_SESSION_ID {value!r}: want {pattern}")

    @classmethod
    def unshown_session(cls, session: str) -> TuiError:
        return cls(f"no terminal shows tmux session {session}")

    @classmethod
    def invalid_session_name(cls, session: object) -> TuiError:
        return cls(f"invalid session name {session!r}: want [A-Za-z0-9_-]+")

    @classmethod
    def invalid_target(cls, target: object) -> TuiError:
        return cls(f"invalid target {target!r}: want a session name [A-Za-z0-9_-]+ or a pane id %N")


class CliError(argparse.ArgumentTypeError):
    @classmethod
    def per_column(cls, value: str) -> CliError:
        return cls(f"want an int from 1 to 9999, not {value!r}")

    @classmethod
    def lines(cls, value: str) -> CliError:
        return cls(f"want a positive int, not {value!r}")
