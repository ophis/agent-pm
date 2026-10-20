"""Delegate-owned quota checks, shared by scheduling and explicit recovery."""

import functools
from collections.abc import Callable

from agent_pm import delegate
from agent_pm.orchestrator.engine.log import log
from agent_pm.orchestrator.engine.runs import limits
from agent_pm.utils import exhausted, summary

BRAKE_5H = 80


def read_usage(role, cfg, *, client=None):
    return _probe(role, cfg, cfg.delegate_cls, client)[0]


def _probe(role, cfg, provider, client):
    """Usage, else None, and the ConfigError that left it unknown."""
    if provider is None:
        return None, None
    overlays = (cfg.core,)
    if client is not None:
        overlays += ({"roles": {role: {"client": client}}},)
    try:
        return provider.usage(role, overlays=overlays), None
    except delegate.ConfigError as error:
        return None, error
    except delegate.SessionError:
        return None, None


class Gate:
    def __init__(self, runs, now, say=None, *, cfg, dry):
        self.cfg = cfg
        self.limits, self.dry, self.seen = limits(runs, now), dry, {}
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=dry)

    def blocked(self, role, ident, *, key, reject_config=False):
        kind, client = key
        if key in self.limits:
            reason, fields = "limit", {"until": self.limits[key].isoformat()}
            if self.dry:
                self.say("usage", client=client, **fields, allowed=False)
        else:
            if client not in self.seen:
                self.seen[client] = self._check(role, client, kind)
            reason, fields, error = self.seen[client]
            if reject_config and error is not None:
                raise error
        if reason:
            self.say("usage-skip", ident, once=True, client=client, reason=reason, **fields)
        return reason is not None

    def _check(self, role, client, kind):
        usage, error = _probe(role, self.cfg, self.cfg.delegates.get(kind), client)
        shown = {"usage": summary(usage)}
        reason = "usage unknown" if usage is None else "usage" if exhausted(usage) else None
        fields = shown if reason == "usage" else {}
        if self.dry:
            self.say("usage", client=client, **shown, allowed=reason is None)
        return reason, fields, error
