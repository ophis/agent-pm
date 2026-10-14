"""The session layer's configuration entry point."""

from agent_pm.utils import LOCAL, read_resources


def read_config(*, local: str = LOCAL) -> dict:
    return read_resources((("agent_pm.session", "config.toml"),), local=local)
