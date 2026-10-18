"""OpenCode's Client, client `opencode`, headless only; the rest is OpenCodeCommon's."""

import os

from agent_pm.session.clients.base import Client
from agent_pm.session.clients.opencode.common import IDENTITY, SELECTORS, VALUED, OpenCodeCommon
from agent_pm.session.clients.opencode.headless import OpenCodeHeadless
from agent_pm.session.engine.types import Spec


class OpenCodeClient(Client):
    selection = ((), ("--session",))
    valued_flags = VALUED
    forbidden_selection = SELECTORS
    keys = frozenset({"flags", "modes", "tiers", "efforts", "env", "ollama_port"})
    client_note = (
        "Pre-approved commands match on their exact text and each shell call starts fresh, so anything else is "
        "refused."
    )
    session_env = "OPENCODE"
    identity_env = IDENTITY

    def __init__(self, config):
        super().__init__(config)
        self.common = OpenCodeCommon(self.config, selector=self.select_session)

    def headless(self):
        return OpenCodeHeadless(self.common)

    def usage_probe(self):
        return self.common.probe()

    def usage(self, output):
        return self.common.parse_usage(output)

    def writable_dirs(self, spec: Spec):
        dirs = (spec.access.cwd, *spec.access.dirs)
        return tuple(dict.fromkeys(map(os.path.realpath, dirs)))
