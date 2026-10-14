"""Explicit subprocess wiring for the fake Linear transport and detached run children."""

import os
import sys
from dataclasses import replace

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import promote, router, run
from agent_pm.session import Session
from orchestrator_tests.integration.fake_transport import install


class DetachedHost:
    def __init__(self, host):
        self.host = host

    def detach(self, name, launch, **placement):
        prefix = (sys.executable, *router.RUN)
        if launch.argv[: len(prefix)] != prefix:
            raise ValueError(f"unexpected detached command: {launch.argv}")
        tests = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        return self.host.detach(
            name,
            replace(
                launch,
                argv=(sys.executable, __file__, "run", *launch.argv[len(prefix) :]),
                env={
                    **launch.env,
                    "PYTHONPATH": os.pathsep.join((launch.env["PYTHONPATH"], tests)),
                },
            ),
            **placement,
        )


class DetachedSession:
    @staticmethod
    def inspect(spec):
        return Session.inspect(spec)

    @staticmethod
    def host(name=None):
        return DetachedHost(Session.host(name))


def main(argv):
    entry, *arguments = argv
    with install(os.environ["FAKE_LINEAR_TRANSPORT"]):
        if entry == "router":
            Session.configure_from(delegate.read_config())
            return router.main(arguments, session=DetachedSession)
        return {"run": run.main, "promote": promote.main}[entry](arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
