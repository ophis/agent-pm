"""Bundled tmux validation operations, independent of the standalone facade."""

from __future__ import annotations

import os
import runpy
from typing import TYPE_CHECKING

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux.commands import (
        session_name,
    )
    from agent_pm.session.hosts.tmux.errors.types import (
        TuiError,
    )
    from agent_pm.session.hosts.tmux.model.types import (
        OPENER,
        RETILES,
        SPLITS,
    )
else:
    _load_file = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_file"]
    _commands = _load_file(
        "agent_pm.session.hosts.tmux.commands", os.path.join(DIRECTORY, "commands.py")
    )
    session_name = _commands.session_name
    _errors_types = _load_file(
        "agent_pm.session.hosts.tmux.errors.types", os.path.join(DIRECTORY, "errors/types.py")
    )
    TuiError = _errors_types.TuiError
    _model_types = _load_file(
        "agent_pm.session.hosts.tmux.model.types", os.path.join(DIRECTORY, "model/types.py")
    )
    OPENER = _model_types.OPENER
    RETILES = _model_types.RETILES
    SPLITS = _model_types.SPLITS


def validate_layout(split: str | None, split_from: str | None, opener: str | None = None) -> None:
    if split is not None and split not in SPLITS:
        raise TuiError.invalid_split(split, SPLITS)
    if split_from is not None:
        session_name(split_from)
    if opener is not None and not OPENER.fullmatch(opener):
        raise TuiError.invalid_opener(opener)


def validate_per_column(per_column: int | None) -> None:
    if per_column is not None and (
        isinstance(per_column, bool)
        or not isinstance(per_column, int)
        or not 1 <= per_column <= 9999
    ):
        raise TuiError.invalid_per_column(per_column)


def validate_retile(retile: str | None) -> None:
    if retile is not None and retile not in RETILES:
        raise TuiError.invalid_retile(retile, RETILES)
