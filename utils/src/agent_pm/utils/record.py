"""Record v1, one JSON line naming a started thing by its outermost starter (ctl queries it, resume
resumes it), and Status, the line `<ctl> status` prints per Record. Display is opaque: only the
session layer interprets it.
"""

import json
from dataclasses import Field, asdict, dataclass, fields
from typing import Any, ClassVar, Self

Display = str | None


class RecordError(ValueError):
    pass


def _text(value) -> bool:
    return isinstance(value, str) and value != "" and value.isprintable()


def _check(*, ok: bool, message: str) -> None:
    if not ok:
        raise RecordError(message)


class _Line:
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    def to_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def parse(cls, line: str) -> Self:
        kind = f"{cls.__name__} line"
        _check(ok=isinstance(line, str), message=f"{kind}: want a str")
        try:
            data = json.loads(line.removesuffix("\n"))
        except (ValueError, RecursionError) as e:
            raise RecordError(f"{kind}: not JSON ({e})") from None
        _check(ok=isinstance(data, dict), message=f"{kind}: want a JSON object")
        names = [f.name for f in fields(cls)]
        for name in names:
            _check(ok=name in data, message=f"{name}: missing")
        for name in data:
            _check(ok=name in names, message=f"{name}: unknown field")
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
        _check(ok=type(self.v) is int and self.v == 1, message="v: want 1")
        for field in ("kind", "name"):
            _check(
                ok=_text(getattr(self, field)), message=f"{field}: want a non-empty printable str"
            )
        _check(
            ok=isinstance(self.sessions, tuple) and all(map(_text, self.sessions)),
            message="sessions: want a list of non-empty printable str",
        )
        for field in ("sid", "workdir", "detail"):
            value = getattr(self, field)
            _check(
                ok=value is None or _text(value),
                message=f"{field}: want null or a non-empty printable str",
            )
        for field in ("ctl", "resume"):
            argv = getattr(self, field)
            _check(
                ok=isinstance(argv, tuple) and bool(argv) and all(isinstance(a, str) for a in argv),
                message=f"{field}: want a non-empty list of str",
            )
            _check(
                ok=bool(argv[0]) and "/" not in argv[0],
                message=f"{field}[0] must be a command name without /",
            )
        _check(
            ok=self.display is None or isinstance(self.display, str),
            message="display: want null or a str",
        )


@dataclass(frozen=True)
class Status(_Line):
    name: str
    alive: bool
    state: str
    outcome: str | None
    attach: str | None

    def __post_init__(self):
        _check(ok=_text(self.name), message="name: want a non-empty printable str")
        _check(ok=isinstance(self.alive, bool), message="alive: want true or false")
        _check(ok=_text(self.state), message="state: want a non-empty printable str")
        for field in ("outcome", "attach"):
            value = getattr(self, field)
            _check(
                ok=value is None or isinstance(value, str), message=f"{field}: want null or a str"
            )
