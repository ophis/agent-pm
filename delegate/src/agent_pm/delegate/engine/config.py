"""Delegate package locations and legacy configuration forwarding."""

import agent_pm.session as session
import agent_pm.utils as utils
from agent_pm.delegate import config
from agent_pm.delegate.config import CONFIG, harness_host, policy, read_config, trusted_dirs


def module_roots() -> tuple[str, ...]:
    return utils.module_roots(config, session, utils)


__all__ = [
    "CONFIG",
    "harness_host",
    "policy",
    "read_config",
    "trusted_dirs",
    "module_roots",
]
