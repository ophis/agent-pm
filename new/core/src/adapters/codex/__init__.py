"""Codex's abstract factory."""
from adapters.base import Adapter, SessionInfo
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

    def usage(self, info=None):
        return self.common.usage()

    def session_info(self, sid, cwd):
        found = self.common.inspect(sid, cwd)
        return SessionInfo(found.sid, found.native_id, found.cwd, found.project, found.transcript, found.resume,
                           found.resumable, found.last_active)
