"""Codex headless launches and native JSON events with session binding and usage limits."""

import json

from agent_pm.adapters.base import (
    Activity,
    Headless,
    Launch,
    LimitHit,
    Reply,
    SessionBound,
    Stderr,
    TurnEnd,
)

from .common import LIMIT_PREFIXES, flags, native_id, reset_at


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
            unset=self.adapter.common.UNSET,
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
