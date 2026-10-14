"""The Linear orchestrator's command entry point."""

import argparse
import sys

from agent_pm.linear_orchestrator.engine import promote, router, run


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="agent-pm-linear")
    parser.add_argument("command", choices=("router", "promote", "run"))
    entry = parser.parse_args(arguments[:1]).command
    return {"router": router.main, "promote": promote.main, "run": run.main}[entry](arguments[1:])


if __name__ == "__main__":
    sys.exit(main())
