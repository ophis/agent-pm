"""Delegate package locations."""

import agent_pm.session as session
import agent_pm.utils as utils
from agent_pm.delegate import config


def module_roots() -> tuple[str, ...]:
    return utils.module_roots(config, session, utils)
