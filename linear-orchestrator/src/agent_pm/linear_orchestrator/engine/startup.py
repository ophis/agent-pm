"""Load orchestrator configuration and select its log directory for a scheduled entry point."""

import sys

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import Config, ConfigError
from agent_pm.linear_orchestrator.linear import linear


def configured(src: str, path: str, *, dry: bool = False) -> Config | None:
    try:
        cfg = config.load(path=path)
    except ConfigError as error:
        if not error.logs_dir:
            print(error.msg, file=sys.stderr)
        else:
            linear.use_logs(error.logs_dir)
            linear.config_error(src, error, dry=dry)
        return None
    linear.use_logs(cfg.logs_dir)
    return cfg
