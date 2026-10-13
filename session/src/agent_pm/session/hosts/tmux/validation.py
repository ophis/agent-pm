"""Bundled tmux validation operations, independent of the standalone facade."""

from __future__ import annotations

import os
import runpy
from typing import TYPE_CHECKING

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux import commands as _commands
    from agent_pm.session.hosts.tmux.errors import types as _errors
    from agent_pm.session.hosts.tmux.model import types as _model
else:
    load_siblings = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_siblings"]
    _commands, _errors, _model = load_siblings(
        DIRECTORY,
        ("agent_pm.session.hosts.tmux.commands", "commands.py"),
        ("agent_pm.session.hosts.tmux.errors.types", "errors/types.py"),
        ("agent_pm.session.hosts.tmux.model.types", "model/types.py"),
    )


def validate_layout(split: str | None, split_from: str | None, opener: str | None = None) -> None:
    if split is not None and split not in _model.SPLITS:
        raise _errors.TuiError.invalid_split(split, _model.SPLITS)
    if split_from is not None:
        _commands.session_name(split_from)
    if opener is not None and not _model.OPENER.fullmatch(opener):
        raise _errors.TuiError.invalid_opener(opener)


def validate_per_column(per_column: int | None) -> None:
    if per_column is not None and (
        isinstance(per_column, bool)
        or not isinstance(per_column, int)
        or not 1 <= per_column <= 9999
    ):
        raise _errors.TuiError.invalid_per_column(per_column)


def validate_retile(retile: str | None) -> None:
    if retile is not None and retile not in _model.RETILES:
        raise _errors.TuiError.invalid_retile(retile, _model.RETILES)
