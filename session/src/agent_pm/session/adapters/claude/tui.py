"""Claude Code interactive launches, merged hooks, transcript replies and native usage limits.
Each new main-thread assistant text in the transcript since launch is one Reply, read from a cursor;
a turn end's text that lands later is not repeated.
"""

import json
import os
import re
import shlex
import stat
import time
from datetime import datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_pm.session.adapters.base import Tui
from agent_pm.session.adapters.claude.common import ClaudeCommon
from agent_pm.session.engine.types import ConfigError, HookKind, LimitHit, Reply, Spec, TurnFailed
from agent_pm.utils import read_regular

TAIL = 4 << 20
LIMIT = re.compile(r"You've hit your ((?:[\w']+ ){0,3})limit|Usage limit reached")
WHEN = re.compile(
    (
        r"(?:([A-Za-z]+)(?: (\d{1,2})(?:, (\d{4}))?)?,? )?(\d{1,2})(?::([0-5]\d))?([ap]m)(?: \(([^)]*)\))?"
    ),
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
    hooks = MappingProxyType(
        {
            "Stop": HookKind.TURN_END,
            "StopFailure": HookKind.TURN_FAILED,
            "PermissionRequest": HookKind.BLOCKED,
            "Notification": HookKind.BLOCKED,
            "PostToolUse": HookKind.ACTIVITY,
            "UserPromptSubmit": HookKind.ACTIVITY,
        }
    )
    MATCHER = (
        "^(permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input"
        "|quota_auto_resume_stale)$"
    )
    dialogs = ("Quick safety check",)

    def __init__(self, common: ClaudeCommon):
        self.common = common
        self.sid = ""
        self.transcript, self.start, self.offset = "", 0, 0
        self.last = self.unseen = ""  # the turn's newest text; its turn end's text not yet read

    def launch(self, spec: Spec, hook, *, source_roots: tuple[str, ...]):
        common = self.common
        self.sid = spec.sid or ""
        hooks = {
            label: [
                {
                    **({"matcher": self.MATCHER} if label == "Notification" else {}),
                    "hooks": [{"type": "command", "command": shlex.join((*hook, label))}],
                }
            ]
            for label in self.hooks
        }
        tail = ("--", spec.prompt) if spec.prompt else ()
        settings, flags = common.merged(spec.flags, hooks)
        launch = common.launch(spec, settings, tail=(*flags, *tail))
        self.transcript = launch.transcript
        self.start = self.offset = _size(launch.transcript)
        return launch

    def hook_state(self):
        return {"adapter_state": {"transcript": self.transcript, "start": self.start}}

    def restore(self, spec, state):
        self.sid = spec.sid or ""
        path, start = state.get("transcript"), state.get("start")
        if not (
            isinstance(path, str) and os.path.isabs(path) and type(start) is int and start >= 0
        ):
            raise ConfigError("claude tui state: bad transcript cursor")
        self.transcript, self.start, self.offset = path, start, start

    def observe(self):
        self.offset = min(self.offset, _size(self.transcript))
        data = read_regular(self.transcript, offset=self.offset) or b""
        end = data.rfind(b"\n") + 1
        self.offset += end
        return [reply for line in data[:end].split(b"\n") for reply in self._record(line)]

    def _record(self, line: bytes) -> list[Reply]:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            return []
        if not isinstance(record, dict) or record.get("isSidechain") is True:
            return []
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("type") == "user":
            if isinstance(content, str) or _texts(content):  # a prompt: a new turn
                self.last = self.unseen = ""
            return []
        if record.get("type") != "assistant" or record.get("isApiErrorMessage") is True:
            return []
        text = "\n".join(_texts(content))
        if not text.strip():
            return []
        if self.unseen and text.strip() == self.unseen.strip():
            self.unseen = ""
            return []
        self.last = text
        return [Reply(text)]

    def session_id(self, payload):
        """The sid it launched or restored: its native id."""
        return self.sid if super().session_id(payload) else ""

    def reply(self, payload):
        text = payload.get("last_assistant_message")
        if not isinstance(text, str):
            path = payload.get("transcript_path")
            try:
                data = read_regular(path, limit=TAIL, tail=True) if isinstance(path, str) else None
            except ValueError:  # a NUL or a lone surrogate in the path
                data = None
            text = _last_text(data or b"")
        self.unseen = text if text.strip() and text != self.last else ""
        self.last = ""
        return text

    def pending(self, payload):
        tasks = payload.get("background_tasks")
        return len(tasks) if isinstance(tasks, list) else 0

    def activity(self, payload):
        return _first(payload, "tool_name")

    def blocked_reason(self, payload):
        return _first(payload, "tool_name", "message")

    def failure(self, payload):
        """StopFailure's typed error: rate_limit is the usage limit, any other a failed turn."""
        error = _first(payload, "error") or "unknown"
        if error == "rate_limit":
            return [LimitHit(_resets_in(_first(payload, "last_assistant_message")))]
        return [TurnFailed(error, _first(payload, "error_details", "last_assistant_message"))]

    def limit(self, screen):
        screen = " ".join(screen.split())
        if not any(m[1] != "fast " for m in LIMIT.finditer(screen)):
            return None
        return LimitHit(_resets_in(screen))


def _first(payload: dict, *keys: str) -> str:
    values = (payload.get(key) for key in keys)
    return next((value for value in values if isinstance(value, str) and value), "")


def _size(path: str) -> int:
    """A regular file's size; 0 for anything else."""
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def _texts(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]


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
        if not isinstance(message, dict) or not isinstance(content, list):
            continue
        blocks = _texts(content)
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


def _resets_in(message: str) -> float | None:
    """When the last usage-limit notice in `message` (no fast-mode one) says it resets."""
    message = " ".join(message.split())
    found = [m for m in LIMIT.finditer(message) if m[1] != "fast "]
    at = message.find("resets ", found[-1].end()) if found else -1
    when = WHEN.match(message, at + len("resets ")) if at >= 0 else None
    return None if when is None else _resets(when)


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
