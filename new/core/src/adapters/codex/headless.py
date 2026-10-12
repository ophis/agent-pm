import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adapters.base import Activity, Headless, Launch, LimitHit, Reply, SessionBound, Stderr, TurnEnd

from .common import flags, native_id

LIMIT_PREFIXES = (
    "You've hit your usage limit.",
    "You've hit your usage limit for ",
    "Quota exceeded. Check your plan and billing details.",
    "Your workspace is out of credits.",
    "You hit your spend cap set in your workspace.",
    "You hit your spend cap set by the owner of your workspace.",
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
RESET = re.compile(
    r"(?:Try|try) again at (?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})"
    r"(?P<suffix>st|nd|rd|th), (?P<year>\d{4}) )?"
    r"(?P<hour>[1-9]|1[0-2]):(?P<minute>[0-5]\d) (?P<period>AM|PM)\.$"
)


def reset_at(message, environment):
    match = RESET.search(message)
    if match is None:
        return None
    try:
        zone_name = environment.get("TZ", os.environ.get("TZ"))
        zone = ZoneInfo(zone_name.removeprefix(":")) if zone_name else None
        value = match.groupdict()
        today = datetime.now(zone)
        year, month, day = today.year, today.month, today.day
        if value["month"]:
            year, month, day = (
                int(value["year"]),
                MONTHS.index(value["month"]) + 1,
                int(value["day"]),
            )
            suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            if value["suffix"] != suffix:
                return None
        hour = int(value["hour"]) % 12 + (12 if value["period"] == "PM" else 0)
        local = datetime(year, month, day, hour, int(value["minute"]))
        stamps = set()
        for fold in (0, 1):
            stamp = local.replace(tzinfo=zone, fold=fold).timestamp()
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None, fold=0) == local:
                stamps.add(stamp)
        return stamps.pop() if len(stamps) == 1 else None
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        return None


class CodexHeadless(Headless):
    def __init__(self, adapter):
        super().__init__(adapter)
        self.state = None
        self.last_reply = ""
        self.limit_sent = False

    def launch(self, prompt, spec, resume):
        self.state = self.adapter.common.prepare(spec, resume)
        self.limit_sent = False
        snapshot = self.state.snapshot
        argv = ["codex", "exec", *flags(snapshot, interactive=False)]
        if resume:
            argv.extend(("resume", self.state.native_id))
        argv.extend(("--", prompt))
        return Launch(
            tuple(argv),
            env=self.adapter.common.environment(snapshot),
            unset=self.adapter.common.unset,
            cwd=snapshot["cwd"],
            project=snapshot["project"],
            transcript=self.adapter.common.transcript(self.state) if resume else "",
            resume=self.adapter.common.resume_command(self.state) if resume else "",
        )

    def events(self, lines):
        for line in lines:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                yield Stderr("codex: invalid JSON event")
                continue
            if isinstance(record, dict):
                yield from self.parse(record)

    def parse(self, record):
        kind = record.get("type")
        if kind == "thread.started":
            found = record.get("thread_id")
            if not native_id(found):
                yield Stderr("codex: invalid thread.started ID")
                return
            self.state = self.adapter.common.bind(self.state.workdir, self.state.sid, found)
            yield SessionBound(found)
        elif kind == "item.completed":
            item = record.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                self.last_reply = item["text"]
                yield Reply(self.last_reply)
            else:
                yield Activity(kind)
        elif kind == "turn.completed":
            yield TurnEnd(self.last_reply)
        elif kind in ("error", "turn.failed"):
            error = record.get("message") if kind == "error" else record.get("error")
            if isinstance(error, dict):
                error = error.get("message")
            if isinstance(error, str) and error.strip():
                yield Stderr(error)
                if not self.limit_sent and error.replace("’", "'").startswith(LIMIT_PREFIXES):
                    self.limit_sent = True
                    yield LimitHit(reset_at(error, self.adapter.common.config.get("env", {})))
        elif isinstance(kind, str):
            yield Activity(kind)
