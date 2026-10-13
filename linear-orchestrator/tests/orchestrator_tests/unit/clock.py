"""Standard-library clock control and production log configuration for tests."""

import contextlib

from agent_pm.linear_orchestrator.linear import linear


@contextlib.contextmanager
def logs_at(path):
    previous = linear.LOGS_DIR
    linear.use_logs(path)
    try:
        yield
    finally:
        linear.use_logs(previous)
