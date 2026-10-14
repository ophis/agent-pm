"""Codex's abstract factory."""

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.codex.common import CodexCommon
from agent_pm.session.adapters.codex.headless import CodexHeadless
from agent_pm.session.adapters.codex.tui import CodexTui
from agent_pm.session.engine.types import SessionInfo, Spec


class CodexAdapter(Adapter):
    selection = ((), ("resume",))
    valued_flags = (
        "-c",
        "--config",
        "-C",
        "--cd",
        "-p",
        "--profile",
        "--enable",
        "--disable",
        "--add-dir",
        "-m",
        "--model",
        "-s",
    )
    forbidden_selection = ("resume", "fork", "--last")
    keys = frozenset({"tiers", "efforts", "env"})
    runtime_note = "Run each shell call separately."
    session_env = "CODEX_THREAD_ID"
    identity_env = tuple(key for key in CodexCommon.UNSET if key != "CLAUDECODE")
    child_defaults = CodexCommon.DEFAULTS

    def __init__(self, config):
        super().__init__(config)
        self.common = CodexCommon(self.config, selector=self.select_session)

    def headless(self):
        return CodexHeadless(self.common)

    def tui(self):
        return CodexTui(self.common)

    def usage_probe(self):
        return self.common.probe()

    def usage(self, output):
        return self.common.parse_usage(output)

    def session_metadata(self, info):
        return self.common.session_metadata(info)

    def session_status(self, info: SessionInfo):
        return self.common.session_status(info.sid, info.cwd, transcript=info.transcript)

    def writable_dirs(self, spec: Spec):
        return self.common.writable_dirs(spec, resume=spec.resume)
