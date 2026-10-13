"""Explicit subprocess wiring for the fake Linear transport and detached run children."""

import os
import sys

from agent_pm.linear_orchestrator.engine import config, promote, router, run
from agent_pm.session import Driver
from orchestrator_tests.integration.fake_transport import install


class DetachedDriver(Driver):
    def detach(self, name, argv, cwd, env, *, opener=None):
        prefix = [sys.executable, *router.RUN]
        if list(argv[: len(prefix)]) != prefix:
            raise ValueError(f"unexpected detached command: {argv}")
        return super().detach(
            name,
            [sys.executable, __file__, "run", *argv[len(prefix) :]],
            cwd,
            {
                **env,
                "PYTHONPATH": os.pathsep.join(
                    (env["PYTHONPATH"], os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
                ),
            },
            opener=opener,
        )


def main(argv):
    entry, *arguments = argv
    with install(os.environ["FAKE_LINEAR_TRANSPORT"]):
        if entry == "router":
            settings = config.load().driver
            driver = DetachedDriver(
                adapters=settings.registry.settings.adapters,
                hosts=settings.registry.settings.hosts,
                default_host=settings.registry.settings.default_host,
            )
            return router.main(arguments, driver=driver)
        return {"run": run.main, "promote": promote.main}[entry](arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
