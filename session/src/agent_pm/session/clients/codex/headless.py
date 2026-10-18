"""Codex headless launches and native JSON events with session binding and usage limits."""

import json
import os

from agent_pm.session.clients.codex.common import (
    LIMIT_PREFIXES,
    CodexCommon,
    launch_flags,
    native_id,
    reset_at,
)
from agent_pm.session.clients.codex.common.types import SessionState
from agent_pm.session.clients.common import split_flags
from agent_pm.session.engine.types import (
    Activity,
    ClientBound,
    Launch,
    LimitHit,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
)
from agent_pm.session.modes.client_adapters.headless import Headless


class CodexHeadless(Headless):
    def _home(self):
        return self.common.home()

    def __init__(self, common: CodexCommon):
        self.common = common
        self.state: SessionState | None = None
        self.last_reply = ""
        self.limit_sent = False

    def launch(self, spec: Spec):
        resume = spec.resume
        self.state = self.common.prepare(spec)
        self.limit_sent = False
        snapshot = self.state.snapshot
        executable = os.path.join(self._runtime.bin_dir, "codex") if self._runtime else "codex"
        mode = self.common.modes[self._permission]
        argv = [executable, "exec", *launch_flags(snapshot, interactive=False, mode=mode)]
        if resume:
            argv.extend(self.common.selector(self.state.native_id, resume=True))
        _, literal = split_flags(snapshot.flags)
        argv.extend((*literal, spec.prompt) if literal else ("--", spec.prompt))
        return Launch(
            tuple(argv),
            env=self.common.environment(snapshot),
            unset=self.common.UNSET,
            cwd=snapshot.cwd,
            project=snapshot.project,
            transcript=self.common.transcript(self.state) if resume else "",
            resume=self.common.resume_command(self.state) if resume else "",
        )

    def commit(self) -> None:
        self.state = self.common.commit(self.state)

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
            yield ClientBound(found)
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
