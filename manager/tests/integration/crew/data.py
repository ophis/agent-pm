"""Shared data fixtures for this topic."""

import json
import os
import shlex
import sys

from agent_pm.manager.engine import monitor

from manager.tests.case import PY, PYTHONPATH, SRC
from session.tests.integration.adapters.codex import fake_codex

CREW = [*PY, "-m", "agent_pm.manager.engine.crew"]


ENTRY = [sys.executable, "-P", "-m", "agent_pm.manager.engine.crew"]


TMUX_HOST = os.path.join(SRC["session"], "agent_pm", "session", "hosts", "tmux", "tmux_host.py")


MANAGER = "mgr"


SECOND = "mgr2"


TIMEOUT = 60


WIDTH = 64


NOTIFY = 'tui.notification_method="bel"'


CODEX_HOOKS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "Interrupt",
)


CLAUDE_HOOKS = {"Stop", "PermissionRequest", "Notification", "PostToolUse", "UserPromptSubmit"}


CALLER = "26900000-0000-4000-8000-000000000009"


ROLE = "dummy-tester"


ROLE_SID = "26700000-0000-4000-8000-000000000005"


ROLE_TUI = f"{ROLE}-{ROLE_SID[:8]}"


ROLE_DRIVER = f"{ROLE_TUI}-drive"


CLAUDE_SESSION = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_SESSION_ID": CALLER,
}


ENV_DUMP = (
    "import json, os, sys; open(sys.argv[1], 'a')"
    ".write(json.dumps({'argv': sys.argv[2:], 'env': dict(os.environ)}) + '\\n')"
)


def codex_session(thread: str = CALLER) -> dict[str, str]:
    return {"CODEX_THREAD_ID": thread, "CODEX_SANDBOX": "seatbelt", **fake_codex.DEFAULTS}


def is_event(line: str, name: str, kind: str) -> bool:
    return bool(monitor.EVENT.fullmatch(line)) and line.split(" ")[1:] == [name, kind]


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[i : i + 2] == [flag, value] for i in range(len(argv)))


def tail(directory: str, n: int = 0, gen: int = 0, *backlog: str) -> str:
    me, events, name = (
        f"env PYTHONPATH={shlex.quote(PYTHONPATH)} python3 -P -m agent_pm.manager.engine.crew",
        shlex.quote(os.path.join(directory, "events")),
        os.path.basename(directory),
    )
    lines = [
        *backlog,
        f"N={n} GEN={gen}",
        f"monitor: tail -n +{n + 1} -F {events}",
        f"monitor expired: {me} attach --manager {name} --after LINE --gen {gen} --resume",
        f"next-event: {me} next-event --manager {name} --after {n} --gen {gen}",
    ]
    return "".join(f"{line}\n" for line in lines)


def raw(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def filler(name: str, count: int) -> list[str]:
    return [f"12:00:00 {name}{i} done ".ljust(WIDTH - 1, ".") for i in range(1, count + 1)]


def logged(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []
