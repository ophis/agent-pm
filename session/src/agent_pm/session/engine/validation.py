"""Validate session invocation strings, environment mappings and native session identifiers."""

import re
from collections.abc import Mapping


def string_tuple(value) -> bool:
    return isinstance(value, tuple) and all(isinstance(s, str) for s in value)


def clean_string(value) -> bool:
    return isinstance(value, str) and "\0" not in value


def env_ok(env) -> bool:
    return isinstance(env, Mapping) and all(
        isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and clean_string(v)
        for k, v in env.items()
    )


def sid_error(sid) -> str | None:
    if isinstance(sid, str) and sid and sid.isprintable() and "/" not in sid:
        return None
    return f'sid must be a non-empty printable string without "/", got {sid!r:.80}'
