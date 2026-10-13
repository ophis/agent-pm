"""Standard-library clock control and production log configuration for tests."""

import contextlib
import datetime
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear


@contextlib.contextmanager
def logs_at(path):
    previous = linear.LOGS_DIR
    linear.use_logs(path)
    try:
        yield
    finally:
        linear.use_logs(previous)


def frozen(value):
    instant = datetime.datetime.fromisoformat(value)

    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    return mock.patch.object(datetime, "datetime", Clock)
