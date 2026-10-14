"""Explicit subprocess wiring for the fake Linear transport: each entry runs under it. `launch`
runs router.outer on a claimed or resumed run given by its Spec fields (--name VALUE); `tui ISSUE
SID ROLE CLIENT DISPLAY INPUT` detaches a recorded run's LinearRun in tui mode at DISPLAY.
"""

import argparse
import functools
import os
import sys

from agent_pm.linear_orchestrator import cli
from agent_pm.linear_orchestrator.engine import client, config, log, promote, router
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.types import Spec
from orchestrator_tests.integration.fake_transport import install


def launch(argv):
    parser = argparse.ArgumentParser(prog="entry.py launch")
    for name in ("uuid", "issue", "project", "assignee", "sid", "mode", "client"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--task")
    cfg = config.load()
    log.use_logs(cfg.logs_dir)
    return router.outer(
        Spec(**vars(parser.parse_args(argv))),
        cfg,
        gql=functools.partial(client.linear_gql, service=cfg.harness_key),
        run=config.shell,
        keychain=client.has_key,
    )


def tui(argv):
    issue, sid, role, run_client, display, text = argv
    cfg = config.load()
    LinearRun(
        issue,
        text,
        cfg.run_dir(issue),
        cfg=cfg,
        role=role,
        sid=sid,
        client=run_client,
        mode="tui",
        display=display,
    ).detach()
    return 0


def main(argv):
    entry, *arguments = argv
    with install(os.environ["FAKE_LINEAR_TRANSPORT"]):
        if entry == "writeback":
            return cli.main([entry, *arguments])
        entries = {"launch": launch, "tui": tui, "promote": promote.main, "router": router.main}
        return entries[entry](arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
