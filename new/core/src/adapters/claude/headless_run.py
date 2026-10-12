"""Claude Code's headless Mode, `claude -p` with stream-json output. launch binds Claude's session id to the sid,
grants each access dir (--add-dir) and command (--allowedTools Bash(<command>), then the config's allow), adds the
project and local setting sources only for a project run, and puts the prompt last after "--" so a leading "-" stays
the prompt. parse turns init, assistant, result and rate-limit records into events, one LimitHit per run."""
import json
import os

from adapters.base import Activity, HeadlessRun, Launch, LimitHit, Reply, SessionBound, TurnEnd
from adapters.claude.common import number


class ClaudeHeadlessRun(HeadlessRun):
    def __init__(self, adapter):
        super().__init__(adapter)
        self.limit_sent = False
        self.resets_at = None

    def launch(self, prompt, spec, resume):
        common = self.adapter.common
        sid = common.uuid(spec.sid)
        model, effort = common.model(spec.tier), common.effort(spec.effort)
        access = spec.access
        cwd = access.cwd or spec.workdir
        mcp = os.path.join(cwd, ".mcp.json")
        allow = (*(f"Bash({command})" for command in access.commands), *common.allow)
        argv = ("claude", "-p", "--resume" if resume else "--session-id", sid,
                *(("--model", model) if model is not None else ()),
                *(("--effort", effort) if effort is not None else ()),
                *common.flags,
                "--setting-sources", "user,project,local" if access.project else "user",
                *(("--mcp-config", mcp) if access.project and os.path.isfile(mcp) else ()),
                "--output-format", "stream-json", "--verbose", "--settings", json.dumps(common.PLUGINS),
                *(arg for d in access.dirs for arg in ("--add-dir", d)),
                *(("--allowedTools", *allow) if allow else ()),
                "--", prompt)
        return Launch(argv, env=common.env(), unset=common.UNSET, transcript=common.transcript(cwd, sid),
                      resume=common.resume(cwd, sid, spec.workdir))

    def parse(self, record):
        kind = record.get("type")
        if kind == "system":
            native = record.get("session_id")
            if record.get("subtype") == "init" and isinstance(native, str) and native:
                yield SessionBound(native)
        elif kind == "rate_limit_event":
            info = record.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                if (resets := number(info.get("resetsAt"))) is not None:
                    self.resets_at = resets
                yield from self._limit()
        elif kind == "assistant":
            if record.get("error") == "rate_limit":
                yield from self._limit()
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            text = "\n".join(b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str))
            if text.strip():
                yield Reply(text)
            for block in blocks:
                if block.get("type") == "tool_use":
                    name = block.get("name")
                    yield Activity(name if isinstance(name, str) else "")
        elif kind == "result":
            result = record.get("result")
            yield TurnEnd(result if isinstance(result, str) else "", 0)

    def _limit(self):
        if not self.limit_sent:
            self.limit_sent = True
            yield LimitHit(self.resets_at)
