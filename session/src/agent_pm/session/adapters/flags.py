"""Runtime argv scanning with option aliases, consumed spans and a literal tail."""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Flag:
    arg: str
    word: str
    value: str | None
    start: int
    end: int

    @property
    def text(self) -> str:
        return f"{self.arg} {self.value}" if self.end - self.start > 1 else self.arg


def split_flags(flags: Sequence[str]) -> tuple[list[str], list[str]]:
    index = flags.index("--") if "--" in flags else len(flags)
    return list(flags[:index]), list(flags[index:])


def scan(flags: Sequence[str], valued: Mapping[str, str] = MappingProxyType({})) -> Iterator[Flag]:
    """Scan before '--'; a valued option may consume that separator as its next value."""
    i = 0
    while i < len(flags) and flags[i] != "--":
        start, arg = i, flags[i]
        if arg.startswith("--"):
            word, attached, value = arg.partition("=")
        else:
            word, attached = arg[:2], arg[2:]
            value = attached.removeprefix("=")
        name = valued.get(word)
        if name is None:
            value = None
        elif not attached:
            i += 1
            value = flags[i] if i < len(flags) else ""
        yield Flag(arg, name or word, value, start, i + 1)
        i += 1
