"""Claude Code's Adapter, runtime `claude`: ClaudeHeadless (headless.py), ClaudeTui (tui.py); the rest is
ClaudeCommon's."""

import os

from adapters.base import Adapter
from adapters.claude.common import ClaudeCommon
from adapters.claude.headless import ClaudeHeadless
from adapters.claude.tui import ClaudeTui


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
        dirs = (spec.access.cwd or spec.workdir, *spec.access.dirs)
        return tuple(dict.fromkeys(map(os.path.realpath, dirs)))
