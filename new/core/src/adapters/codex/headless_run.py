import json

from adapters.base import Activity, HeadlessRun, Launch, LimitHit, Reply, SessionBound, Stderr, TurnEnd
from .common import flags, native_id

LIMIT_PREFIXES = ("You've hit your usage limit.", "You've hit your usage limit for ",
                  "Your workspace is out of credits.",
                  "You hit your spend cap set in your workspace.",
                  "You hit your spend cap set by the owner of your workspace.")


class CodexHeadlessRun(HeadlessRun):
    def __init__(self, adapter):
        super().__init__(adapter)
        self.state = None
        self.last_reply = ""

    def launch(self, prompt, spec, resume):
        self.state = self.adapter.common.prepare(spec, resume)
        snapshot = self.state.snapshot
        argv = ["codex", "exec", *flags(snapshot, interactive=False)]
        if resume:
            argv.extend(("resume", self.state.native_id))
        argv.append(prompt)
        return Launch(tuple(argv), env=self.adapter.common.environment(snapshot),
                      unset=self.adapter.common.unset, cwd=snapshot["cwd"],
                      resume=self.adapter.common.resume_command(self.state) if resume else "")

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
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
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
                if error.replace("’", "'").startswith(LIMIT_PREFIXES):
                    # exec --json drops the structured reset; its localized display time lacks a timezone.
                    yield LimitHit()
        elif isinstance(kind, str):
            yield Activity(kind)
