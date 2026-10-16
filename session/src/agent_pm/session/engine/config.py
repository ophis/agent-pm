"""The session layer's configuration entry point."""

from agent_pm.utils import LOCAL, read_resources

CONFIG_RESOURCES = (("agent_pm.session", "config.toml"),)


def read_config(*, local: str = LOCAL) -> dict:
    return read_resources(CONFIG_RESOURCES, local=local)
