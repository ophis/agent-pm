"""DelegateEvent v1, the callback line: one JSON line per harness event on a callback's stdin, holding only its kind's fields.
A wire object: outcome and limit stay plain dicts (an Outcome's and a Limit's fields)."""

import json
from dataclasses import dataclass, fields
from typing import Self

KINDS = ("begun", "progress", "finished")
_FIELDS = {
    "begun": ("sid",),
    "progress": ("name", "text"),
    "finished": ("returncode", "outcome", "error", "limit"),
}
_KIND = f"kind: want one of {', '.join(KINDS)}"


class DelegateEventError(ValueError):
    pass


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise DelegateEventError(message)


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@dataclass(frozen=True)
class DelegateEvent:
    kind: str
    sid: str | None = None
    name: str | None = None
    text: str | None = None
    returncode: int | None = None
    outcome: dict | None = None
    error: str = ""
    limit: dict | None = None

    def __post_init__(self):
        _check(self.kind in KINDS, _KIND)
        for each in fields(self)[1:]:
            if each.name not in _FIELDS[self.kind]:
                _check(
                    getattr(self, each.name) == each.default,
                    f"{each.name}: not a {self.kind} field",
                )
        if self.kind == "begun":
            _check(isinstance(self.sid, str) and self.sid != "", "sid: want a non-empty str")
        elif self.kind == "progress":
            for field in ("name", "text"):
                _check(isinstance(getattr(self, field), str), f"{field}: want a str")
        else:
            _check(type(self.returncode) is int, "returncode: want an int")
            outcome, limit = self.outcome, self.limit
            _check(
                outcome is None
                or (isinstance(outcome, dict) and isinstance(outcome.get("status"), str)),
                "outcome: want null or an object with a str status",
            )
            _check(isinstance(self.error, str), "error: want a str")
            _check(
                limit is None
                or (
                    isinstance(limit, dict)
                    and limit.keys() == {"resets_at"}
                    and (limit["resets_at"] is None or _number(limit["resets_at"]))
                ),
                'limit: want null or {"resets_at": a number or null}',
            )

    def to_line(self) -> str:
        data = {"v": 1, "kind": self.kind} | {f: getattr(self, f) for f in _FIELDS[self.kind]}
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def parse(cls, line: str) -> Self:
        _check(isinstance(line, str), "DelegateEvent line: want a str")
        try:
            data = json.loads(line.removesuffix("\n"))
        except (ValueError, RecursionError) as e:
            raise DelegateEventError(f"DelegateEvent line: not JSON ({e})") from None
        _check(isinstance(data, dict), "DelegateEvent line: want a JSON object")
        _check(type(data.get("v")) is int and data["v"] == 1, "v: want 1")
        kind = data.get("kind")
        _check(kind in KINDS, _KIND)
        for key in data:
            _check(key in ("v", "kind", *_FIELDS[kind]), f"{key}: not a {kind} field")
        return cls(**{k: v for k, v in data.items() if k != "v"})
