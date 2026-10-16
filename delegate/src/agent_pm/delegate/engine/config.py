"""Delegate configuration resources and package locations."""

from collections.abc import Mapping

import agent_pm.session as session
import agent_pm.utils as utils
from agent_pm.delegate import config


def read_config(*, local: str = utils.LOCAL, path: str | None = None) -> dict:
    def validate(package: str, source: str, table: Mapping) -> None:
        if package == "agent_pm.delegate":
            config.validate_team_dirs(table, source, local=local)

    return utils.read_resources(
        (*session.CONFIG_RESOURCES, ("agent_pm.delegate", "config.toml")),
        local=local,
        paths=None if path is None else {"agent_pm.delegate": path},
        validate=validate,
    )


def module_roots() -> tuple[str, ...]:
    return utils.module_roots(config, session, utils)
