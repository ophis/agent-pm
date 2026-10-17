"""Attach targets: what the Viewer shows a session through, named by its Host."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionView:
    """An existing session the Viewer places as it is."""

    session: str


@dataclass(frozen=True)
class CommandView:
    """A command the Viewer runs in a view session it makes: printed, the attach command shown to
    the user; argv, what that view session runs.
    """

    printed: str
    argv: tuple[str, ...]
