"""The Docker Host's live gate, run by hand from a manager session: `python3
session/tests/live_docker.py CWD [NAME]` times build, worker start and first event.
"""

import os
import re
import subprocess
import sys
import time

from agent_pm.session.hosts.docker.broker import Broker, plugin_root

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO, "bin")
WAIT = 300


def run(*argv: str, timeout: float = WAIT) -> str:
    res = subprocess.run(
        [os.path.join(BIN, argv[0]), *argv[1:]],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    if res.returncode:
        raise SystemExit(f"{' '.join(argv)}: exit {res.returncode}: {res.stderr.strip()}")
    return res.stdout


def timed(times: dict, step: str, action):
    started = time.monotonic()
    found = action()
    times[step] = time.monotonic() - started
    return found


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2) or not os.path.isabs(argv[0]):
        print(__doc__, file=sys.stderr)
        return 2
    cwd, name = argv[0], argv[1] if len(argv) == 2 else "docker-live"
    manager = os.environ.get("AGENT_PM_MANAGER")
    if not manager:
        raise SystemExit("run from a manager session: AGENT_PM_MANAGER is unset")
    times: dict[str, float] = {}
    broker = Broker()
    broker.version()
    timed(times, "build", lambda: broker.build(plugin_root(os.path.join(BIN, "agent-pm-session"))))
    cursor = run("agent-pm-crew", "attach", "--manager", manager)
    after, gen = (re.search(rf"\b{key}=(\d+)", cursor) for key in ("N", "GEN"))
    if after is None or gen is None:
        raise SystemExit(f"agent-pm-crew attach printed no N= and GEN=: {cursor!r:.200}")
    timed(
        times,
        "container start and client update",
        lambda: run(
            *("agent-pm-crew", "worker", "--manager", manager, name),
            *("--client", "claude", "--cwd", cwd, "--display", "none"),
            *("--prompt", "Reply with the single word ready."),
        ),
    )
    try:
        timed(
            times,
            "client ready (first event)",
            lambda: run(
                *("agent-pm-crew", "next-event", "--manager", manager),
                *("--after", after.group(1), "--gen", gen.group(1)),
            ),
        )
        run("agent-pm-session", "send", name, "Reply with the single word again.")
        print(run("agent-pm-session", "read", name))
    finally:
        timed(times, "close", lambda: run("agent-pm-crew", "close", "--manager", manager, name))
    for step, seconds in times.items():
        print(f"{step}: {seconds:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
