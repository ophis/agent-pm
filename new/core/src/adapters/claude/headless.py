"""Claude Code's headless Mode, `claude -p` with stream-json output: ClaudeHeadless.

launch is ClaudeCommon's with -p, the stream-json output flags and the plugin settings, and the prompt last after "--",
so a prompt starting with "-" stays the prompt.

parse, a field of the wrong type counting as absent: system init → SessionBound(session_id); assistant → Reply(its
text blocks joined by newlines) unless blank, then Activity(name) per tool_use block; result → TurnEnd(result); a
rejected rate_limit_event or an assistant rate_limit error → LimitHit(the last rejected event's resetsAt), once per
run."""
from adapters.base import Activity, Headless, LimitHit, Reply, SessionBound, TurnEnd
from adapters.claude.common import number


class ClaudeHeadless(Headless):
    def __init__(self, adapter):
        super().__init__(adapter)
        self.limit_sent = False
        self.resets_at = None

    def launch(self, prompt, spec, resume):
        common = self.adapter.common
        return common.launch(spec, resume, common.PLUGINS, ("-p",), ("--output-format", "stream-json", "--verbose"),
                             ("--", prompt))

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
