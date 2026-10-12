"""Codex's abstract factory."""

from adapters.base import Adapter, SessionStatus

from .common import CodexCommon
from .headless import CodexHeadless
from .tui import CodexTuiRun


class CodexAdapter(Adapter):
    keys = frozenset({"tiers", "efforts", "shell_network", "sandbox", "env"})
    runtime_note = (
        "Run each shell call separately. Command approvals follow Codex automatic review."
    )
    session_env = "CODEX_THREAD_ID"
    identity_env = tuple(key for key in CodexCommon.unset if key != "CLAUDECODE")
    child_defaults = CodexCommon.defaults

    def __init__(self, config):
        super().__init__(config)
        self.common = CodexCommon(self.config)

    def headless(self):
        return CodexHeadless(self)

    def tui(self):
        return CodexTuiRun(self)

    def usage_probe(self):
        return self.common.probe()

    def usage(self, output):
        return self.common.parse_usage(output)

    def session_status(self, sid, cwd):
        found = self.common.inspect(sid, cwd)
        return SessionStatus(found.resumable, found.last_active, found.resume)

    def writable_dirs(self, spec, resume):
        return self.common.writable_dirs(spec, resume)
