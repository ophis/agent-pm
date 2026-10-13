"""Shared expectations for orchestrator configuration."""

import os
import shlex

from agent_pm.linear_orchestrator.engine import config
from agent_pm.utils import import_roots

GATE = (
    f"env PYTHONPATH={shlex.quote(os.pathsep.join(import_roots(config.ROOT)))} "
    "python3 -P -m agent_pm.linear_orchestrator.engine.router --brake researcher"
)
