"""Session invocation names, timing and tmux placement validation."""

from agent_pm.session.engine.types import (
    ConfigError,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.errors.types import TuiError


def validate_idle_timeout(idle_timeout) -> None:
    if idle_timeout is not None and (
        isinstance(idle_timeout, bool)
        or not isinstance(idle_timeout, (int, float))
        or not idle_timeout > 0
    ):
        raise ConfigError.idle_timeout(idle_timeout)


def validate_name(name) -> str:
    if not isinstance(name, str) or not tmux_host.NAME.fullmatch(name):
        raise ConfigError.session_name(name)
    return name


def validate_events(events, *, host=tmux_host) -> str | None:
    if events is None:
        return None
    if not isinstance(events, str) or not events.isprintable():
        raise ConfigError.events_path(events)
    try:
        return host.events_file(events)
    except TuiError as e:
        raise ConfigError(str(e)) from None


def validate_placement(split, split_from, opener=None) -> None:
    if split is not None and split not in tmux_host.SPLITS:
        raise ConfigError.split_direction(split)
    if split_from is not None and not (
        isinstance(split_from, str) and tmux_host.NAME.fullmatch(split_from)
    ):
        raise ConfigError.split_source(split_from)
    if opener is not None and not (isinstance(opener, str) and tmux_host.OPENER.fullmatch(opener)):
        raise ConfigError.opener(opener)
