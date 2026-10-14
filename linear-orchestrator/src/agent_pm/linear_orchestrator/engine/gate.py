"""Delegate-owned quota checks, shared by scheduling and explicit recovery."""

import functools
from collections.abc import Callable

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine.log import log
from agent_pm.linear_orchestrator.engine.runs import limits

MAX_5H = 90
BRAKE_5H = 80
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


def read_usage(role, *, delegate_cls):
    try:
        return delegate_cls.usage(role)
    except (delegate.ConfigError, delegate.DriverError):
        return None


class Gate:
    def __init__(self, runs, now, say=None, *, cfg, dry):
        self.cfg = cfg
        self.limits, self.dry, self.seen = limits(runs, now), dry, {}
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=dry)

    def blocked(self, role, ident, *, key=None):
        current = (self.cfg.delegate_kind, self.cfg.delegate_cls.resolve(role).runtime)
        key = current if key is None else key
        kind, runtime = key
        if key in self.limits:
            reason, fields = "limit", {"until": self.limits[key].isoformat()}
            if self.dry:
                self.say("usage", runtime=runtime, **fields, allowed=False)
        elif key != current:
            self.say(
                "usage-drift",
                ident,
                runtime=runtime,
                delegate_kind=kind,
                current_runtime=current[1],
                current_delegate_kind=current[0],
            )
            return False
        else:
            if key not in self.seen:
                self.seen[key] = self._check(role, runtime)
            reason, fields = self.seen[key]
        if reason:
            self.say("usage-skip", ident, once=True, runtime=runtime, reason=reason, **fields)
        return reason is not None

    def _check(self, role, runtime):
        usage = read_usage(role, delegate_cls=self.cfg.delegate_cls)
        shown = {"usage": summary(usage)}
        reason = "usage unknown" if usage is None else "usage" if exhausted(usage) else None
        fields = shown if reason == "usage" else {}
        if self.dry:
            self.say("usage", runtime=runtime, **shown, allowed=reason is None)
        return reason, fields
