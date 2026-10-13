"""Claude Code's Adapter, runtime `claude`: ClaudeHeadless (headless.py), ClaudeTui (tui.py); the
rest is ClaudeCommon's.
"""

import os

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.claude.common import ClaudeCommon, added_dirs
from agent_pm.session.adapters.claude.headless import ClaudeHeadless
from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.engine.types import Spec


class ClaudeAdapter(Adapter):
    selection = (("--session-id",), ("--resume",))
    selection_uuid = True
    valued_flags = (
        "--model",
        "--effort",
        "--name",
        "--settings",
        "--add-dir",
        "--setting-sources",
        "--mcp-config",
    )
    forbidden_selection = (
        "--resume",
        "-r",
        "--session-id",
        "--continue",
        "-c",
        "--fork-session",
        "--from-pr",
        "--teleport",
    )
    keys = frozenset({"flags", "allow", "tiers", "efforts", "env"})
    runtime_note = ClaudeCommon.NOTE
    session_env = "CLAUDECODE"
    identity_env = ClaudeCommon.UNSET

    def __init__(self, config):
        super().__init__(config)
        self.common = ClaudeCommon(self.config, selector=self.select_session)

    def headless(self):
        return ClaudeHeadless(self.common)

    def tui(self):
        return ClaudeTui(self.common)

    def usage_probe(self):
        return self.common.probe()

    def usage(self, output):
        return self.common.parse_usage(output)

    def session_status(self, sid, cwd):
        return self.common.session_status(sid, cwd)

    def writable_dirs(self, spec: Spec, *, resume: bool = False):
        cwd = spec.access.cwd or spec.workdir
        dirs = (cwd, *spec.access.dirs, *(os.path.join(cwd, d) for d in added_dirs(spec.flags)))
        return tuple(dict.fromkeys(map(os.path.realpath, dirs)))
