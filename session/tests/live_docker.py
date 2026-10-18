"""The Docker Host's live gate, run by hand from a manager session: `python3
session/tests/live_docker.py CWD [NAME]` times build, worker start and first event, then kills
the hook supervisor during a turn and checks that the turn's event waits in the relay queue, that
`read` restarts the supervisor, and that the event then comes exactly once.
"""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

from agent_pm.session.hosts.docker.broker import Broker, plugin_root

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO, "bin")
WAIT = 300
QUIET = 30
AUDIT = os.path.expanduser("~/Library/Logs/agent-pm/broker.jsonl")
GITHUB = "github-installation-token"
CLIENT_KIND = {"claude": "claude-setup-token", "codex": "codex-access-token"}
BOT = "app/agent-pm-bot"
PHASES = ("checks", "image", "start", "relay", "tmux", "client")
BROKER_PHASES = frozenset(
    ("preflight", "image", "checks", "create", "bootstrap", "clients", "credentials", "total")
)
BROKER_LINE = re.compile(r"^agent-pm-broker: start (\S+) ([0-9.]+) s$", re.MULTILINE)
# Resource → the command docs/credentials.md › What a container cannot reach runs as `sh -c`.
UNREACHABLE = {
    "host keychain": r"find / \( -path /proc -o -path /sys \) -prune -o -path "
    r'"*/Library/Keychains*" -print 2>/dev/null | wc -l; command -v security | wc -c',
    "Linear keys": r"env | grep -ci linear; find / \( -path /proc -o -path /sys \) -prune -o "
    r"-name config.local.toml -print 2>/dev/null | wc -l",
    "host `gh` login": r"find / \( -path /proc -o -path /sys \) -prune -o -path "
    r'"*/gh/hosts.yml" -print 2>/dev/null | wc -l; env | grep -c "^GH_TOKEN="',
    "Docker socket": r"find / \( -path /proc -o -path /sys \) -prune -o -type s -name "
    r'"*docker*" -print 2>/dev/null | wc -l; command -v docker | wc -c; '
    r'env | grep -c "^DOCKER_HOST="',
}


def audit_lines(text: str) -> list[dict]:
    found = []
    for raw in text.splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def _credentials(lines: list[dict], *events: str) -> list[dict]:
    return [
        c
        for x in lines
        if not events or x.get("event") in events
        for c in x.get("credentials") or []
        if isinstance(c, dict)
    ]


def _tokens(credentials: list[dict]) -> dict[tuple, dict]:
    """Each installation's last GitHub token entry."""
    return {
        (c.get("installation"), c.get("account")): c for c in credentials if c.get("kind") == GITHUB
    }


def reclaimed(lines: list[dict], generation: str, client: str) -> list[str]:
    mine = [x for x in lines if x.get("generation") == generation]
    closes = [i for i, x in enumerate(mine) if x.get("event") == "close"]
    if not closes:
        return [f"{generation}: no close line in the audit log"]
    after = mine[closes[0] :]
    problems = []
    tokens = _tokens(_credentials(after, "close", "revocation-retry"))
    if not tokens:
        problems.append(f"{generation}: close revoked no {GITHUB}")
    for (_, account), entry in tokens.items():
        if entry.get("result") != "revoked":
            reason = f" ({entry.get('reason')})" if entry.get("result") == "pending" else ""
            problems.append(f"{generation}: {GITHUB} of {account} {entry.get('result')}{reason}")
    kind = CLIENT_KIND[client]
    if not any(
        c.get("kind") == kind and c.get("result") == "withdrawn"
        for c in _credentials(after, "close")
    ):
        problems.append(f"{generation}: close left {kind} not withdrawn")
    return problems


def removed_tokens(lines: list[dict], generation: str) -> str:
    tokens = _tokens(_credentials([x for x in lines if x.get("generation") == generation]))
    revoked = sum(e.get("result") == "revoked" for e in tokens.values())
    if tokens and revoked == len(tokens):
        return f"removed {generation}: github tokens revoked {revoked}/{len(tokens)}"
    expires = max((str(e.get("expires_at")) for e in tokens.values()), default="n/a")
    return (
        f"removed {generation}: github tokens not revoked (revoked {revoked}/{len(tokens)}),"
        f" expires {expires}"
    )


def _related(a: str, b: str) -> bool:
    a, b = os.path.normpath(a), os.path.normpath(b)
    return os.path.commonpath([a, b]) in (a, b)


def baseline(inspect: dict, name: str, cwd: str, plan_mounts: list[str]) -> list[str]:
    host, config = inspect.get("HostConfig") or {}, inspect.get("Config") or {}
    problems = []
    if "ALL" not in (host.get("CapDrop") or []):
        problems.append("HostConfig.CapDrop lacks ALL")
    if not any(str(o).startswith("no-new-privileges") for o in host.get("SecurityOpt") or []):
        problems.append("HostConfig.SecurityOpt lacks no-new-privileges")
    if config.get("User") != "agent":
        problems.append("Config.User is not agent")
    if host.get("Privileged"):
        problems.append("HostConfig.Privileged is true")
    problems += [
        f"HostConfig.{k} is host"
        for k in ("NetworkMode", "PidMode", "IpcMode")
        if host.get(k) == "host"
    ]
    mounts = inspect.get("Mounts") or []
    volumes = [m for m in mounts if m.get("Type") == "volume"]
    if [(v.get("Name"), v.get("Destination")) for v in volumes] != [(f"agent-pm-ws-{name}", cwd)]:
        problems.append(f"want one volume, agent-pm-ws-{name} at {cwd}")
    binds = [m for m in mounts if m.get("Type") == "bind"]
    risky = []
    for m in binds:
        source, target = m.get("Source", ""), m.get("Destination", "")
        if _related(source, cwd):
            risky.append(f"bind {source} overlaps the cwd {cwd}")
        elif "docker.sock" in source or "docker.sock" in target:
            risky.append(f"bind {source} mounts docker.sock")
    if not risky and sorted(m.get("Source", "") for m in binds) != sorted(plan_mounts):
        problems.append("bind mounts differ from the plan's mounts")
    return problems + risky


def zero_counts(output: str) -> bool:
    counts = [x.strip() for x in output.splitlines() if x.strip()]
    return bool(counts) and all(x == "0" for x in counts)


def _seconds(pattern: str, stderr: str) -> str:
    found = re.search(pattern, stderr, re.MULTILINE)
    return f"{float(found[1]):.1f} s" if found else "n/a"


def _ready(result: dict, hooks: str) -> str:
    if not result.get("launched_at"):
        return "n/a"
    launched = datetime.fromisoformat(result["launched_at"])
    for record in audit_lines(hooks):
        ts = record.get("ts")
        if isinstance(ts, str) and (at := datetime.fromisoformat(ts)) > launched:
            return f"{(at - launched).total_seconds():.1f} s"
    return "n/a"


def timing(result: dict, hooks: str, stderr: str, name: str) -> str:
    parts = [f"{phase} {seconds:.1f} s" for phase, seconds in (result.get("phases") or {}).items()]
    parts.append(f"client ready {_ready(result, hooks)}")
    parts.append(
        f"preflight {_seconds(rf'^docker timing {re.escape(name)}: preflight ([0-9.]+) s$', stderr)}"
    )
    if isinstance(update := result.get("client_update"), dict):
        took = _seconds(r"^agent-pm-broker: start client_update ([0-9.]+) s$", stderr)
        parts.append(f"client_update {update.get('result')} {took}")
    return " | ".join(parts)


def refreshed_at(lines: list[dict], container: str) -> str | None:
    for x in lines:
        if (
            x.get("event") == "refresh"
            and x.get("container") == container
            and any(c.get("result") == "renewed" for c in _credentials([x]))
        ):
            return x.get("time")
    return None


def new_generation(before: set[str], after: set[str], *, step: str = "start") -> str:
    new = after - before
    if len(new) != 1:
        raise SystemExit(f"{step}: want one new generation, got {len(new)}")
    return next(iter(new))


def install_steps(result: dict, stderr: str, client: str) -> list[str]:
    problems = [f"install step {p} at start" for p in result.get("phases") or {} if p not in PHASES]
    allowed = BROKER_PHASES | ({"client_update"} if client == "codex" else set())
    problems += [
        f"install step {p} at start" for p, _ in BROKER_LINE.findall(stderr) if p not in allowed
    ]
    return problems


def opened_draft(events: list[str], *, is_draft: bool) -> bool:
    for event in events:
        if event in ("ready_for_review", "convert_to_draft"):
            return event == "ready_for_review"
    return is_draft


def bot_pr(
    view: dict, events: list[str], refreshed: str | None, *, need_refresh: bool
) -> list[str]:
    problems = []
    login = (view.get("author") or {}).get("login")
    if login != BOT:
        problems.append(f"PR author is {login!r}, want {BOT}")
    if not opened_draft(events, is_draft=bool(view.get("isDraft"))):
        problems.append("PR was not opened as a draft")
    if need_refresh and refreshed is None:
        problems.append("no token refresh recorded")
    elif need_refresh and datetime.fromisoformat(view["createdAt"]) <= datetime.fromisoformat(
        refreshed
    ):
        problems.append(
            f"PR created at {view['createdAt']}, before the token refresh at {refreshed}"
        )
    return problems


def pending_except(status: dict, removed: str | None) -> list[str]:
    return [
        f"pending revocation for {e.get('container')} ({e.get('account')})"
        for e in status.get("revocations") or []
        if e.get("container") != removed
    ]


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
        "container start",
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
