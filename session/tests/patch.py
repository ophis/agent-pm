"""Patched(prefix, **fakes): a patcher (start/stop or `with`) that replaces, in each loaded module under `prefix`, every
name bound to agent_pm.session.<key> (an alias too) by fakes[key]. Upper layers import the session's functions by name,
so patching agent_pm.session itself would miss them."""

import sys
from contextlib import ExitStack
from unittest import mock

import agent_pm.session as session


class Patched:
    def __init__(self, prefix: str, **fakes):
        self.prefix, self.fakes, self.stack = prefix, fakes, None

    def start(self):
        real = {id(getattr(session, key)): fake for key, fake in self.fakes.items()}
        self.stack = ExitStack()
        for name, module in list(sys.modules.items()):
            if name == self.prefix or name.startswith(self.prefix + "."):
                for attr, value in list(vars(module).items()):
                    if id(value) in real:
                        self.stack.enter_context(mock.patch.object(module, attr, real[id(value)]))
        return self

    def stop(self):
        self.stack.close()

    __enter__ = start

    def __exit__(self, *exc):
        self.stop()
