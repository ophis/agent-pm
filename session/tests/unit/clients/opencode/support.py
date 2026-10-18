"""OpenCode unit fixtures: the packaged [clients.opencode] table and a session id."""

import os
from types import SimpleNamespace

from agent_pm.session.clients import registry
from agent_pm.session.engine.config import read_config

SES = "ses_0123456789abABCDEFGHIJKLMN"


def packaged_table() -> dict:
    """The packaged session config.toml's [clients.opencode], without implementation."""
    table = dict(registry.settings(read_config(local=os.devnull))["clients"]["opencode"])
    table.pop("implementation")
    return table


def fake_git(argv: list[str], *_: object, **__: object) -> SimpleNamespace:
    """subprocess.run of git rev-parse in a dir holding a .git dir: its own toplevel."""
    top = argv[argv.index("-C") + 1]
    ok = os.path.isdir(os.path.join(top, ".git"))
    out = top + "\n" if "--show-toplevel" in argv else ".git\n.git\n"
    return SimpleNamespace(returncode=0 if ok else 128, stdout=out if ok else "", stderr="fatal")
