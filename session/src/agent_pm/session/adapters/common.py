"""What the Claude and Codex adapters share."""

from collections.abc import Mapping

from agent_pm.session.engine.types import EFFORTS, ConfigError

TIERS = ("1", "2", "3", "4")


def _names(value, keys) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(k, str) and k in keys and isinstance(v, str) and v.strip() and v.isprintable()
        for k, v in value.items()
    )


def _environment(value) -> bool:
    return (
        isinstance(value, Mapping)
        and all(isinstance(k, str) and k and "=" not in k and "\0" not in k for k in value)
        and all(isinstance(v, str) and "\0" not in v for v in value.values())
    )


CHECKS = (
    ("tiers", lambda v: _names(v, TIERS), 'a table from tier "1"–"4" to a model name'),
    (
        "efforts",
        lambda v: _names(v, EFFORTS),
        f"a table from effort ({', '.join(EFFORTS)}) to a printable value",
    ),
    ("env", _environment, "a table of environment names to strings without NUL"),
)


def check(config: Mapping, table: str, checks=CHECKS) -> None:
    for key, ok, what in checks:
        if key in config and not ok(config[key]):
            raise ConfigError(f"{table}.{key} must be {what}")
