import re

from agent_pm.session.adapters.flags import scan
from agent_pm.session.engine.types import ConfigError

UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def select(
    native: str,
    syntax: tuple[tuple[str, ...], tuple[str, ...]],
    *,
    resume: bool,
    uuid: bool = False,
) -> tuple[str, ...]:
    if uuid and not UUID.fullmatch(native):
        raise ConfigError(f"session ids are UUIDs, got {native!r:.80}")
    prefix = syntax[int(resume)]
    return (*prefix, native) if prefix else ()


def check(flags: tuple[str, ...], forbidden: tuple[str, ...], valued: tuple[str, ...] = ()) -> None:
    for flag in scan(flags, {word: word for word in valued}):
        if flag.arg in forbidden or flag.word in forbidden:
            raise ConfigError(f"{flag.arg}: picks the session; use sid= or resume=")
