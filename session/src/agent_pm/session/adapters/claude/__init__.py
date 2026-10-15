"""Claude Code's Adapter, client `claude`: ClaudeHeadless (headless.py), ClaudeTui (tui.py); the
rest is ClaudeCommon's.
"""

import os
from collections.abc import Mapping
from types import MappingProxyType

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.claude.common import ClaudeCommon, added_dirs
from agent_pm.session.adapters.claude.headless import ClaudeHeadless
from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.engine.types import SessionInfo, Spec
from agent_pm.utils import LOCAL_ENV_VARS


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
    client_note = ClaudeCommon.NOTE
    session_env = "CLAUDECODE"
    identity_env = tuple(key for key in ClaudeCommon.UNSET if key not in LOCAL_ENV_VARS)

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

    def resume_main(self, argv):
        return self.common.resume_main(argv)

    def session_status(self, info: SessionInfo, *, env: Mapping[str, str] = MappingProxyType({})):
        return self.common.session_status(info.sid, info.cwd, transcript=info.transcript, env=env)

    def writable_dirs(self, spec: Spec):
        cwd = spec.access.cwd
        dirs = (cwd, *spec.access.dirs, *(os.path.join(cwd, d) for d in added_dirs(spec.flags)))
        return tuple(dict.fromkeys(map(os.path.realpath, dirs)))
