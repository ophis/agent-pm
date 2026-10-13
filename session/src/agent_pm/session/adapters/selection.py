import re

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
        raise ConfigError(f"session ids are UUIDs, got {native!r:.80}")
    prefix = syntax[int(resume)]
    return (*prefix, native) if prefix else ()


def check(flags: tuple[str, ...], forbidden: tuple[str, ...], valued: tuple[str, ...] = ()) -> None:
    i = 0
    while i < len(flags) and flags[i] != "--":
        flag = flags[i]
        word, eq, _ = flag.partition("=") if flag.startswith("--") else (flag[:2], flag[2:], "")
        if flag in forbidden or word in forbidden:
            raise ConfigError(f"{flag}: picks the session; use sid= or resume=")
        i += 2 if word in valued and not eq else 1
