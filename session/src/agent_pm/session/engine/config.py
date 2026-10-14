"""The session layer's configuration entry point."""

from agent_pm.utils import LOCAL, read_project


def read_config(*, local: str = LOCAL) -> dict:
    return read_project("session", local=local)
