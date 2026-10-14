"""Shared expectations for orchestrator configuration."""

import shlex

from agent_pm.linear_orchestrator.engine import config

GATE = (
    f"env PYTHONPATH={shlex.quote(config.pythonpath())} "
    "python3 -P -m agent_pm.linear_orchestrator.cli router --brake researcher"
)
