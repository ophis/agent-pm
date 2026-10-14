"""Explicit subprocess wiring for the fake Linear transport: each entry runs under it."""

import os
import sys

from agent_pm.linear_orchestrator import cli
from agent_pm.linear_orchestrator.engine import promote, router, run
from orchestrator_tests.integration.fake_transport import install


def main(argv):
    entry, *arguments = argv
    with install(os.environ["FAKE_LINEAR_TRANSPORT"]):
        if entry == "writeback":
            return cli.main([entry, *arguments])
        return {"run": run.main, "promote": promote.main, "router": router.main}[entry](arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
