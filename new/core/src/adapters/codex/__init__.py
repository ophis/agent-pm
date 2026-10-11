"""Codex's abstract factory."""
from adapters.base import Adapter, SessionStatus
from .common import CodexCommon
from .headless_run import CodexHeadlessRun


class CodexAdapter(Adapter):
    keys = frozenset({"tiers", "efforts", "shell_network", "env"})
    runtime_note = "Run each shell call separately. Command approvals follow Codex automatic review."

    def __init__(self, config):
        super().__init__(config)
        self.common = CodexCommon(self.config)

    def headless_run(self):
        return CodexHeadlessRun(self)

    def tui_run(self):
        return None

    def usage_probe(self):
        return self.common.probe()

    def usage(self, output):
        return self.common.parse_usage(output)

    def session_status(self, sid, cwd):
        found = self.common.inspect(sid, cwd)
        return SessionStatus(found.resumable, found.last_active, found.resume)
