"""The Docker Host's live gate, run by hand from a manager session: `python3
session/tests/live_docker.py CWD [NAME]` times build, worker start and first event, then kills
the hook supervisor during a turn and checks that the turn's event waits in the relay queue, that
`read` restarts the supervisor, and that the event then comes exactly once.
"""

import fcntl
import os
import re
import signal
import subprocess
import sys
import time

from agent_pm.session.hosts.docker.broker import Broker, plugin_root

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO, "bin")
WAIT = 300
QUIET = 30


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


def supervisor_running(name: str) -> bool:
    path = os.path.expanduser(f"~/.agent-pm/docker-state/{name}/supervisor.lock")
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def kill_supervisor(name: str) -> None:
    with open(os.path.expanduser(f"~/.agent-pm/docker-state/{name}/supervisor.pid")) as f:
        os.kill(int(f.read()), signal.SIGKILL)
    deadline = time.monotonic() + 10
    while supervisor_running(name):
        if time.monotonic() > deadline:
            raise SystemExit("supervisor.lock still held after killing the supervisor")
        time.sleep(0.2)


def quiet(events: tuple[str, ...], after: str) -> str:
    try:
        return run(*events, "--after", after, timeout=QUIET)
    except subprocess.TimeoutExpired:
        return ""


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
    events = ("agent-pm-crew", "next-event", "--manager", manager, "--gen", gen.group(1))
    try:
        first = timed(
            times,
            "client ready (first event)",
            lambda: run(*events, "--after", after.group(1)),
        )
        cursor = first.split()[0]
        run("agent-pm-session", "send", name, "Count from 1 to 200, one number per line.")
        kill_supervisor(name)
        if quiet(events, cursor):
            raise SystemExit("an event arrived while the supervisor was gone")
        run("agent-pm-session", "read", name)
        if not supervisor_running(name):
            raise SystemExit("read did not restart the supervisor")
        line = run(*events, "--after", cursor).split()[0]
        if extra := quiet(events, line):
            raise SystemExit(f"a second event after the restart: {extra!r:.200}")
        print(run("agent-pm-session", "read", name))
    finally:
        timed(times, "close", lambda: run("agent-pm-crew", "close", "--manager", manager, name))
    for step, seconds in times.items():
        print(f"{step}: {seconds:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
