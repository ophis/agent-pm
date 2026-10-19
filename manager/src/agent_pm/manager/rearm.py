"""The plugin's SessionStart command hook (manager/hooks/hooks.json): in a manager session under
`agent-pm-crew hold`, prints the event-monitor re-arm prompt as additionalContext for the
startup, resume, compact and clear sources; anything else prints nothing. Always exits 0.
Standalone: the standard library only, run by path.
`--mode manual` replaces the monitoring tail with turn-based reads and a notifying-tool fallback.
"""

import json
import os
import re
import sys
from collections.abc import Mapping

SOURCES = frozenset({"startup", "resume", "compact", "clear"})
MANAGER_NAME = re.compile(r"[A-Za-z0-9_-]+")  # agent-pm-crew's own rule (cli.py _roster)

PROMPT = """\
agent-pm: SessionStart `{source}` in manager `{name}`'s session (under \
`agent-pm-crew hold`). Your crew event monitor may have stopped. Re-arm it now, before other \
work. These steps are complete without /agent-pm:manage; they follow its Start step 3, Events \
and Recover.

1. Stop your own listener if one is still running (an event listener or background task running \
`agent-pm-crew next-event`).
2. Run `agent-pm-crew attach`. Its last line `N=<N> GEN=<GEN>` is the handled cursor; the \
backlog is every event line after N.
   Lease error (`stale holder generation`, `holder no longer alive`, or another holder is \
active): follow /agent-pm:manage › Recover; the error's `crew:` lines give the exact commands. \
Another holder active: tell the user which. Never run `hold --force` yourself.
3. Read the backlog with `agent-pm-crew pending --content`. In line order: an event whose \
durable result (outcome, sent message, issue state or existing pull request) shows it already \
handled, ack at once; otherwise handle it, then ack. Ack: \
`agent-pm-crew ack --through <line> --gen <GEN>`, GEN from your most recent `attach`. Leave an \
unresolved event and everything after it pending.
4. Run `agent-pm-crew attach` again.
{monitoring}"""

AUTOMATIC = """\
5. Arm only with the `monitor:` command this second `attach` printed (its N and GEN). Never arm \
from a line number you remember or from the last line printed.
   Monitor may be a deferred tool: if its schema is not loaded, load it first (ToolSearch \
`select:Monitor`). Run the `monitor:` command with Monitor, `timeout_ms` 1800000. Never run \
`--follow` with Bash `run_in_background`.
6. When that Monitor ends or expires, repeat from step 1.
"""

MANUAL = """\
5. Manual monitoring; no idle notifications. Read: agent-pm-crew pending --content. \
Recovery: agent-pm-crew attach. Run `agent-pm-crew pending --content` at the beginning of every \
active turn before status reports or new launches. Handle and ack as in step 3.
6. Only if a background tool's runtime contract guarantees a notification to this conversation, \
run the `next-event:` command this second `attach` printed (its N and GEN) as that tool's task, \
without `--follow`. After handling each event, rerun that command. Never arm from a line number \
you remember or from the last line printed. On reader exit, notification failure or expiration, \
repeat from step 1; without a notifying tool, keep Manual monitoring.
"""


def manager(environ: Mapping[str, str]) -> str | None:
    name = environ.get("AGENT_PM_MANAGER", "")
    if not MANAGER_NAME.fullmatch(name) or not environ.get("AGENT_PM_HOLDER_ID"):
        return None
    return None if "AGENT_PM_AGENT_SESSION" in environ else name


def prompt(name: str, source: str, *, manual: bool = False) -> str:
    return PROMPT.format(name=name, source=source, monitoring=MANUAL if manual else AUTOMATIC)


def main(argv: tuple[str, ...] = ()) -> int:
    if argv not in ((), ("--mode", "manual")):
        return 0
    name = manager(os.environ)
    if name is None:
        return 0
    # Raw fds: a closed or broken stdout then fails here, not in a flush at exit.
    try:
        chunks = []
        while chunk := os.read(0, 65536):
            chunks.append(chunk)
        event = json.loads(b"".join(chunks).decode("utf-8"))
        source = event.get("source") if isinstance(event, dict) else None
        if not isinstance(source, str) or source not in SOURCES:
            return 0
        context = {
            "hookEventName": "SessionStart",
            "additionalContext": prompt(name, source, manual=bool(argv)),
        }
        data = (json.dumps({"hookSpecificOutput": context}) + "\n").encode()
        while data:
            data = data[os.write(1, data) :]
    except (OSError, ValueError, RecursionError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv[1:])))
