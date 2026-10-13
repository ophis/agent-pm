"""Claude Code interactive launches, merged hooks, transcript replies and native usage limits."""

import json
import re
import shlex
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_pm.session.adapters.base import ACTIVITY, BLOCKED, TURN_END, Tui
from agent_pm.session.engine.types import LimitHit
from agent_pm.utils import read_regular

TAIL = 4 << 20
LIMIT = re.compile(r"You've hit your ((?:[\w']+ ){0,3})limit|Usage limit reached")
WHEN = re.compile(
    r"(?:([A-Za-z]+)(?: (\d{1,2})(?:, (\d{4}))?)?,? )?(\d{1,2})(?::([0-5]\d))?([ap]m)(?: \(([^)]*)\))?",
    re.I,
)
ZONE = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}")
MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class ClaudeTui(Tui):
    hooks = {
        "Stop": TURN_END,
        "PermissionRequest": BLOCKED,
        "Notification": BLOCKED,
        "PostToolUse": ACTIVITY,
        "UserPromptSubmit": ACTIVITY,
    }
    MATCHER = (
        "^(permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input"
        "|quota_auto_resume_stale)$"
    )
    dialogs = ("Quick safety check",)

    def launch(self, prompt, spec, resume, hook):
        common = self.adapter.common
        hooks = {
            label: [
                {
                    **({"matcher": self.MATCHER} if label == "Notification" else {}),
                    "hooks": [{"type": "command", "command": shlex.join((*hook, label))}],
                }
            ]
            for label in self.hooks
        }
        tail = ("--", prompt) if prompt else ()
        settings, flags = common.merged(spec.flags, hooks)
        return common.launch(spec, resume, settings, tail=(*flags, *tail))

    def reply(self, payload):
        message = payload.get("last_assistant_message")
        if isinstance(message, str):
            return message
        path = payload.get("transcript_path")
        try:
            data = read_regular(path, limit=TAIL, tail=True) if isinstance(path, str) else None
        except ValueError:  # a NUL or a lone surrogate in the path
            data = None
        return _last_text(data or b"")

    def pending(self, payload):
        tasks = payload.get("background_tasks")
        return len(tasks) if isinstance(tasks, list) else 0

    def activity(self, payload):
        return _first(payload, "tool_name")

    def blocked_reason(self, payload):
        return _first(payload, "tool_name", "message")

    def limit(self, screen):
        screen = " ".join(screen.split())
        found = [m for m in LIMIT.finditer(screen) if m[1] != "fast "]
        if not found:
            return None
        at = screen.find("resets ", found[-1].end())
        when = WHEN.match(screen, at + len("resets ")) if at >= 0 else None
        return LimitHit(None if when is None else _resets(when))


def _first(payload: dict, *keys: str) -> str:
    values = (payload.get(key) for key in keys)
    return next((value for value in values if isinstance(value, str) and value), "")


def _last_text(data: bytes) -> str:
    """The text blocks of the last assistant message with text, over its records sharing one
    message.id.
    """
    last, texts = None, []
    for line in reversed(data.split(b"\n")):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        message = (
            record.get("message")
            if isinstance(record, dict) and record.get("type") == "assistant"
            else None
        )
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        blocks = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        mid = message.get("id")
        mid = mid if isinstance(mid, str) and mid else object()
        if last is None:
            if blocks:
                last, texts = mid, blocks
        elif mid != last:
            break
        else:
            texts = blocks + texts
    return "\n".join(texts)


def _index(word: str, names: tuple[str, ...]) -> int | None:
    word = word.lower()
    return next((i for i, name in enumerate(names) if word in (name, name[:3])), None)


def _zone(name: str | None) -> ZoneInfo | None:
    if name and ZONE.fullmatch(name):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return None


def _resets(when: re.Match) -> float | None:
    word, day, year, hour, minute, half, name = when.groups()
    if not 1 <= int(hour) <= 12:
        return None
    hour, minute = int(hour) % 12 + (12 if half.lower() == "pm" else 0), int(minute or 0)
    zone = _zone(name)
    now = datetime.fromtimestamp(time.time(), zone)
    try:
        if day is not None:
            month = _index(word, MONTHS)
            if month is None:
                return None
            at = datetime(int(year or now.year), month + 1, int(day), hour, minute, tzinfo=zone)
            if year is None and at < now - timedelta(days=1):
                at = at.replace(year=now.year + 1)
            return at.timestamp()
        weekday = None if word is None else _index(word, WEEKDAYS)
        if word is not None and weekday is None:
            return None
        for ahead in range(8):
            on = now.date() + timedelta(days=ahead)
            at = datetime(on.year, on.month, on.day, hour, minute, tzinfo=zone)
            if at > now and weekday in (None, at.weekday()):
                return at.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
