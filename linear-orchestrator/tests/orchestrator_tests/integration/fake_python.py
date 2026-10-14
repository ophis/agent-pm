"""A `python3` for bin/agent-pm-linear in tests: runs `-P -m MODULE ARGS` as python3 would, under
the fake Linear transport ($FAKE_LINEAR_TRANSPORT), so the router and its write-back callback reach
only the fake service. Its shim adds the test roots to PYTHONPATH.
"""

import os
import runpy
import sys

from orchestrator_tests.integration.fake_transport import install


def main(argv):
    if argv[:2] != ["-P", "-m"] or len(argv) < 3:
        sys.exit(f"fake_python.py: want -P -m MODULE [ARG ...], got {argv}")
    sys.argv = [argv[2], *argv[3:]]
    with install(os.environ["FAKE_LINEAR_TRANSPORT"]):
        runpy.run_module(argv[2], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main(sys.argv[1:])
