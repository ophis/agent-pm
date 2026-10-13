"""Codex headless launches and native JSON events with session binding and usage limits."""

import json

from agent_pm.session.adapters.base import Headless
from agent_pm.session.adapters.codex.common import (
    LIMIT_PREFIXES,
    CodexCommon,
    flags,
    native_id,
    reset_at,
)
from agent_pm.session.adapters.codex.sessions import SessionState
from agent_pm.session.adapters.flags import split_flags
from agent_pm.session.engine.types import (
    Activity,
    Launch,
    LimitHit,
    Reply,
    SessionBound,
    Spec,
    Stderr,
    TurnEnd,
)


class CodexHeadless(Headless):
    def __init__(self, common: CodexCommon):
        self.common = common
        self.state: SessionState | None = None
        self.last_reply = ""
        self.limit_sent = False

    def launch(self, prompt, spec: Spec, *, resume: bool = False):
        self.state = self.common.prepare(spec, resume=resume)
        self.limit_sent = False
        snapshot = self.state.snapshot
        argv = ["codex", "exec", *flags(snapshot, interactive=False)]
        if resume:
            argv.extend(self.common.selector(self.state.native_id, resume=True))
        _, literal = split_flags(snapshot["flags"])
        argv.extend((*literal, prompt) if literal else ("--", prompt))
        return Launch(
            tuple(argv),
            env=self.common.environment(snapshot),
            unset=self.common.UNSET,
            cwd=snapshot["cwd"],
            project=snapshot["project"],
            transcript=self.common.transcript(self.state) if resume else "",
            resume=self.common.resume_command(self.state) if resume else "",
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
            if not isinstance(found, str) or not native_id(found):
                yield Stderr("codex: invalid thread.started ID")
                return
            if self.state is None:
                yield Stderr("codex: thread.started before launch")
                return
            self.state = self.common.bind(self.state.workdir, self.state.sid, found)
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
            yield from self._error(record, kind)
        elif isinstance(kind, str):
            yield Activity(kind)

    def _error(self, record, kind):
        error = record.get("message") if kind == "error" else record.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str) and error.strip():
            yield Stderr(error)
            if not self.limit_sent and error.replace("’", "'").startswith(LIMIT_PREFIXES):
                self.limit_sent = True
                yield LimitHit(reset_at(error, self.common.config.get("env", {})))
