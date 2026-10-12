"""Record v1, one JSON line naming a started thing by its outermost starter (ctl queries it, resume resumes it), and
Status, the line `<ctl> status` prints per Record. Display is opaque: only the session layer interprets it."""

import json
from dataclasses import asdict, dataclass, fields
from typing import Self

Display = str | None


class RecordError(ValueError):
    pass


def _text(value) -> bool:
    return isinstance(value, str) and value != "" and value.isprintable()


def _check(ok: bool, field: str, want: str) -> None:
    if not ok:
        raise RecordError(f"{field}: want {want}")


class _Line:
    def to_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def parse(cls, line: str) -> Self:
        kind = f"{cls.__name__} line"
        _check(isinstance(line, str), kind, "a str")
        try:
            data = json.loads(line.removesuffix("\n"))
        except ValueError as e:
            raise RecordError(f"{kind}: not JSON ({e})") from None
        _check(isinstance(data, dict), kind, "a JSON object")
        names = [f.name for f in fields(cls)]
        for name in names:
            if name not in data:
                raise RecordError(f"{name}: missing")
        for name in data:
            if name not in names:
                raise RecordError(f"{name}: unknown field")
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
        _check(isinstance(self.v, int) and not isinstance(self.v, bool) and self.v == 1, "v", "1")
        for field in ("kind", "name"):
            _check(_text(getattr(self, field)), field, "a non-empty printable str")
        _check(
            isinstance(self.sessions, tuple) and all(map(_text, self.sessions)),
            "sessions",
            "a list of non-empty printable str",
        )
        for field in ("sid", "workdir", "detail"):
            value = getattr(self, field)
            _check(value is None or _text(value), field, "null or a non-empty printable str")
        for field in ("ctl", "resume"):
            argv = getattr(self, field)
            _check(
                isinstance(argv, tuple) and bool(argv) and all(isinstance(a, str) for a in argv),
                field,
                "a non-empty list of str",
            )
            if not argv[0] or "/" in argv[0]:
                raise RecordError(f"{field}[0] must be a command name without /")
        _check(self.display is None or isinstance(self.display, str), "display", "null or a str")


@dataclass(frozen=True)
class Status(_Line):
    name: str
    alive: bool
    state: str
    outcome: str | None
    attach: str | None

    def __post_init__(self):
        _check(_text(self.name), "name", "a non-empty printable str")
        _check(isinstance(self.alive, bool), "alive", "true or false")
        _check(_text(self.state), "state", "a non-empty printable str")
        for field in ("outcome", "attach"):
            value = getattr(self, field)
            _check(value is None or isinstance(value, str), field, "null or a str")
