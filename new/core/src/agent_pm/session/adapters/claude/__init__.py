"""Claude Code's Adapter, runtime `claude`: ClaudeHeadless (headless.py), ClaudeTui (tui.py); the rest is
ClaudeCommon's."""

import os

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.claude.common import ClaudeCommon, added_dirs
from agent_pm.session.adapters.claude.headless import ClaudeHeadless
from agent_pm.session.adapters.claude.tui import ClaudeTui


class ClaudeAdapter(Adapter):
    keys = frozenset({"flags", "allow", "tiers", "efforts", "env"})
    runtime_note = ClaudeCommon.NOTE
    session_env = "CLAUDECODE"
    identity_env = ClaudeCommon.UNSET

    def __init__(self, config):
        super().__init__(config)
        self.common = ClaudeCommon(self.config)

    def headless(self):
        return ClaudeHeadless(self)

    def tui(self):
        return ClaudeTui(self)

    def usage_probe(self):
        return self.common.probe()

    def usage(self, output):
        return self.common.parse_usage(output)

    def session_status(self, sid, cwd):
        return self.common.session_status(sid, cwd)

    def writable_dirs(self, spec, resume):
        cwd = spec.access.cwd or spec.workdir
        if spec.role_policy:
            dirs = (cwd, *spec.access.dirs)
        else:
            dirs = (cwd, *(os.path.join(cwd, d) for d in added_dirs(spec.flags)))
        return tuple(dict.fromkeys(map(os.path.realpath, dirs)))
