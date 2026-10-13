import functools
from collections.abc import Callable

from agent_pm.linear_orchestrator.engine.runs import limits
from agent_pm.linear_orchestrator.linear.linear import (
    log,
)
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError

MAX_5H = 90


MAX_WEEK = 100


def summary(usage):
    if usage is None:
        return "unknown"
    return " ".join(
        f"{k}={'none' if w is None else f'{w.used_percent:g}%'}"
        for k, w in (("five_hour", usage.five_hour), ("weekly", usage.weekly))
    )


def exhausted(usage, max_5h=MAX_5H):
    five, week = usage.five_hour, usage.weekly
    return (five is not None and five.used_percent >= max_5h) or (
        week is not None and week.used_percent >= MAX_WEEK
    )


def read_usage(runtime, *, driver):
    try:
        return driver.usage(runtime)
    except (SessionConfigError, DriverError, OSError, ValueError, RuntimeError):
        return None


class Gate:
    def __init__(self, runs, now, say=None, *, driver, dry):
        self.driver = driver
        self.limits, self.dry, self.seen = limits(runs, now), dry, {}
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=dry)

    def blocked(self, runtime, ident):
        if runtime not in self.seen:
            self.seen[runtime] = self._check(runtime)
        reason, fields = self.seen[runtime]
        if reason:
            self.say("usage-skip", ident, once=True, runtime=runtime, reason=reason, **fields)
        return reason is not None

    def _check(self, runtime):
        if (at := self.limits.get(runtime)) is not None:
            shown = {"until": at.isoformat()}
            reason, fields = "limit", shown
        else:
            usage = read_usage(runtime, driver=self.driver)
            shown = {"usage": summary(usage)}
            reason = "usage unknown" if usage is None else "usage" if exhausted(usage) else None
            fields = shown if reason == "usage" else {}
        if self.dry:
            self.say("usage", runtime=runtime, **shown, allowed=reason is None)
        return reason, fields
