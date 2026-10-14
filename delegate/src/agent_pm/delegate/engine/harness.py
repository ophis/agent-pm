"""The detached Delegate process, loaded from its workdir's harness.json invocation."""

import argparse
import json
import os
import signal
import sys

from agent_pm.delegate.engine import detach
from agent_pm.delegate.engine.application import Application
from agent_pm.delegate.engine.types import Stopped
from agent_pm.session import SIGNALS


def main(argv: list[str], *, application=None) -> int:
    parser = argparse.ArgumentParser(prog="delegate harness")
    parser.add_argument("workdir")
    args = parser.parse_args(argv)
    try:
        with open(os.path.join(args.workdir, "harness.json"), encoding="utf-8") as stream:
            invocation = argparse.Namespace(**json.load(stream))
    except (OSError, ValueError) as error:
        print(f"delegate: harness state: {error}", file=sys.stderr)
        return 2
    status = None
    try:
        for signum in SIGNALS:
            signal.signal(signum, _stop)
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
            owner = Application() if application is None else application
            code, status = owner.run_cli(invocation)
            return code
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    except Stopped as stopped:
        return stopped.code
    finally:
        detach.outcome(invocation.events, invocation.harness, status)


def _stop(signum, _frame):
    signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    raise Stopped(128 + signum)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
