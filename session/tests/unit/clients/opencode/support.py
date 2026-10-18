"""OpenCode unit fixtures: the packaged [clients.opencode] table and a session id."""

import os

from agent_pm.session.clients import registry
from agent_pm.session.engine.config import read_config

SES = "ses_0123456789abABCDEFGHIJKLMN"


def packaged_table() -> dict:
    """The packaged session config.toml's [clients.opencode], without implementation."""
    table = dict(registry.settings(read_config(local=os.devnull))["clients"]["opencode"])
    table.pop("implementation")
    return table
