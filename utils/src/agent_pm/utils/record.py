"""Record v1, one JSON line naming a started thing by its outermost starter (ctl queries it, resume
resumes it), and Status, the line `<ctl> status` prints per Record. Display is opaque: only the
session layer interprets it."""

import json
from dataclasses import asdict, dataclass, fields
from typing import Self

Display = str | None


class RecordError(ValueError):
    pass


def _text(value) -> bool:
    return isinstance(value, str) and value != "" and value.isprintable()


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise RecordError(message)


class _Line:
    def to_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def parse(cls, line: str) -> Self:
        kind = f"{cls.__name__} line"
        _check(isinstance(line, str), f"{kind}: want a str")
        try:
            data = json.loads(line.removesuffix("\n"))
        except (ValueError, RecursionError) as e:
            raise RecordError(f"{kind}: not JSON ({e})") from None
        _check(isinstance(data, dict), f"{kind}: want a JSON object")
        names = [f.name for f in fields(cls)]
        for name in names:
            _check(name in data, f"{name}: missing")
        for name in data:
            _check(name in names, f"{name}: unknown field")
        return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in data.items()})


@dataclass(frozen=True)
class Record(_Line):
    v: int
    kind: str
    name: str
    sessions: tuple[str, ...]
    sid: str | None
    workdir: str | None
    ctl: tuple[str, ...]
    resume: tuple[str, ...]
    display: Display
    detail: str | None

    def __post_init__(self):
        _check(type(self.v) is int and self.v == 1, "v: want 1")
        for field in ("kind", "name"):
            _check(_text(getattr(self, field)), f"{field}: want a non-empty printable str")
        _check(
            isinstance(self.sessions, tuple) and all(map(_text, self.sessions)),
            "sessions: want a list of non-empty printable str",
        )
        for field in ("sid", "workdir", "detail"):
            value = getattr(self, field)
            _check(
                value is None or _text(value), f"{field}: want null or a non-empty printable str"
            )
        for field in ("ctl", "resume"):
            argv = getattr(self, field)
            _check(
                isinstance(argv, tuple) and bool(argv) and all(isinstance(a, str) for a in argv),
                f"{field}: want a non-empty list of str",
            )
            _check(
                bool(argv[0]) and "/" not in argv[0], f"{field}[0] must be a command name without /"
            )
        _check(self.display is None or isinstance(self.display, str), "display: want null or a str")


@dataclass(frozen=True)
class Status(_Line):
    name: str
    alive: bool
    state: str
    outcome: str | None
    attach: str | None

    def __post_init__(self):
        _check(_text(self.name), "name: want a non-empty printable str")
        _check(isinstance(self.alive, bool), "alive: want true or false")
        _check(_text(self.state), "state: want a non-empty printable str")
        for field in ("outcome", "attach"):
            value = getattr(self, field)
            _check(value is None or isinstance(value, str), f"{field}: want null or a str")
