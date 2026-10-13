import re
from collections.abc import Iterator

from agent_pm.session.engine.types import ConfigError

UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def select(
    native: str,
    resume: bool,
    syntax: tuple[tuple[str, ...], tuple[str, ...]],
    *,
    uuid: bool = False,
) -> tuple[str, ...]:
    if uuid and not UUID.fullmatch(native):
        raise ConfigError.session_uuid(native)
    prefix = syntax[int(resume)]
    return (*prefix, native) if prefix else ()


def scan(flags: tuple[str, ...], valued: tuple[str, ...] = ()) -> Iterator[tuple[str, str, str]]:
    """(arg, --name or -x, value) per arg before "--"; a bare `valued` word takes the next."""
    i = 0
    while i < len(flags) and flags[i] != "--":
        flag = flags[i]
        word, eq, value = flag.partition("=") if flag.startswith("--") else (flag[:2], "", flag[2:])
        eq = eq or value
        if word in valued and not eq:
            i += 1
            value = flags[i] if i < len(flags) else ""
        yield flag, word, value
        i += 1


def check(flags: tuple[str, ...], forbidden: tuple[str, ...], valued: tuple[str, ...] = ()) -> None:
    for flag, word, _ in scan(flags, valued):
        if flag in forbidden or word in forbidden:
            raise ConfigError.session_selection(flag)
