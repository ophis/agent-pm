"""Report configuration failures after the scheduled entry point selects its logging destination."""

import sys

from agent_pm.linear_orchestrator.engine.types import ConfigError
from agent_pm.linear_orchestrator.linear import linear


def report_error(src: str, error: ConfigError, *, dry: bool = False) -> None:
    if not error.logs_dir:
        print(error.msg, file=sys.stderr)
    else:
        linear.config_error(src, error, dry=dry)
