"""Session behavior tests with caller-owned dependencies."""

import json
import os
import shlex
import tempfile
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.claude.common import ClaudeCommon as UnconfiguredCommon
from agent_pm.session.adapters.common import select
from agent_pm.session.engine.types import (
    ConfigError,
    HookKind,
    Launch,
    Usage,
)
from agent_pm.session.engine.types import Spec as BaseSpec
from agent_pm.utils import LOCAL_ENV_VARS

from session.tests.case import Case


class ClaudeCommon(UnconfiguredCommon):
    def __init__(self, config):
        super().__init__(
            config,
            selector=lambda native, resume: select(
                native, ClaudeAdapter.selection, uuid=True, resume=resume
            ),
        )


Spec = BaseSpec

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"

CEILING = {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"}

UNSET = (
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_JOB_DIR",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    *sorted(LOCAL_ENV_VARS),
)

PROBE = (
    "claude",
    "-p",
    "Reply with OK.",
    "--model",
    "haiku",
    "--output-format",
    "stream-json",
    "--verbose",
    "--setting-sources",
    "user",
    "--strict-mcp-config",
)

NOTE = (
    "Pre-approved commands match on their exact text and each shell call starts fresh, so anything else waits on a "
    "permission prompt."
)

CONFIG = {
    "flags": ["--permission-mode", "auto"],
    "allow": ["Read", "Edit(/x)"],
    "tiers": {"1": "fable", "4": "haiku"},
    "efforts": {"low": "lo", "max": "mx"},
    "env": {"K": "v"},
}

WHAT = {
    "flags": "a list of strings",
    "allow": "a list of strings",
    "tiers": 'a table from tier "1"–"4" to a model name',
    "efforts": "a table from effort (low, medium, high, xhigh, max) to a printable value",
    "env": "a table of environment names to strings without NUL",
}


class Base(Case):
    def setUp(self):
        self.tmp = self.dir()
        self.config_dir = os.path.join(self.tmp, "config")
        self.env({"CLAUDE_CONFIG_DIR": self.config_dir})
        self.work = os.path.join(self.tmp, "work")
        os.mkdir(self.work)

    def dir(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return os.path.realpath(tmp.name)

    def env(self, values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)

    def refused(self, call, message):
        with self.assertRaises(ConfigError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)

    def launch(self, prompt="hi", config=CONFIG, *, resume=False, **spec) -> Launch:
        spec = Spec(
            **{"workdir": self.work, "sid": SID, "prompt": prompt, "resume": resume, **spec}
        )
        return ClaudeAdapter(config).headless().launch(spec)


def stream_line(record) -> str:
    return json.dumps(record)


def assistant(*blocks, **fields) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}, **fields}


def text(value) -> dict:
    return {"type": "text", "text": value}


def tool(name) -> dict:
    return {"type": "tool_use", "id": "t", "name": name, "input": {}}


def rejected(resets=1700000000, status="rejected") -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status, "resetsAt": resets}}


INIT = {"type": "system", "subtype": "init", "session_id": SID}


def event(status="allowed", five=0.1, kind="five_hour", resets=1, **week) -> str:
    """router_test's rate_limit_event line, its rateLimitType and resetsAt given."""
    windows = {"five_hour": {"utilization": five, "resetsAt": 1}}
    windows.update({k: {"utilization": v, "resetsAt": 2} for k, v in week.items()})
    return info_line(
        {
            "status": status,
            "utilization": five,
            "rateLimitType": kind,
            "resetsAt": resets,
            "unifiedWindows": windows,
        }
    )


def info_line(info) -> str:
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def unified(**found) -> str:
    """An allowed rate_limit_event line whose unifiedWindows are `found`."""
    return info_line({"status": "allowed", "unifiedWindows": found})


def usage(*lines) -> Usage | None:
    return ClaudeCommon.parse_usage("".join(f"{x}\n" for x in lines))


PROBE_REPLY = {"type": "assistant", "message": {"content": [text("OK")]}, "session_id": SID}
PROBE_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "OK",
    "session_id": SID,
}


def probe_init(source) -> dict:
    return {**INIT, "cwd": "/probe", "apiKeySource": source, "claude_code_version": "2.1.299"}


def stdout(*records) -> str:
    return "".join(f"{json.dumps(record)}\n" for record in records)


# Handwritten Claude 2.1.299 probe stdout: no recordings exist (TASK-421).
API_KEY_PROBE = stdout(probe_init("ANTHROPIC_API_KEY"), PROBE_REPLY, PROBE_RESULT)
SUBSCRIPTION_PROBE = stdout(
    probe_init("none"),
    PROBE_REPLY,
    {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed",
            "unifiedWindows": {
                "five_hour": {"utilization": 0.25, "resetsAt": 1760000000},
                "seven_day": {"utilization": 0.4, "resetsAt": 1760500000},
            },
        },
    },
    PROBE_RESULT,
)


HOOK = ("/py thon", "-I", "/core/tui_hook.py", "/tui/w 1")

HOOKS = {
    "Stop": HookKind.TURN_END,
    "StopFailure": HookKind.TURN_FAILED,
    "PermissionRequest": HookKind.BLOCKED,
    "Notification": HookKind.BLOCKED,
    "PostToolUse": HookKind.ACTIVITY,
    "UserPromptSubmit": HookKind.ACTIVITY,
}

MATCHER = "^(permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input|quota_auto_resume_stale)$"

WAITING = (
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
    "quota_auto_resume_stale",
)

NOT_WAITING = (
    "idle_prompt",
    "auth_success",
    "elicitation_complete",
    "elicitation_response",
    "agent_completed",
    "quota_auto_resume_fired",
    "quota_auto_resume_disabled",
    "",
    "permission_prompt_x",
    "xidle_prompt",
)


def entry(label) -> dict:
    return {"hooks": [{"type": "command", "command": shlex.join((*HOOK, label))}]}


TUI_HOOKS = {
    "Stop": [entry("Stop")],
    "StopFailure": [entry("StopFailure")],
    "PermissionRequest": [entry("PermissionRequest")],
    "Notification": [{"matcher": MATCHER, **entry("Notification")}],
    "PostToolUse": [entry("PostToolUse")],
    "UserPromptSubmit": [entry("UserPromptSubmit")],
    "SessionStart": [{"matcher": "^(startup|resume)$", **entry("SessionStart")}],
}

TUI_SETTINGS = json.dumps({"hooks": TUI_HOOKS})

NAME = "w-1"

WANT_SETTINGS = "want a JSON object with a list of hooks per event"


def store() -> str:
    return os.path.join(os.path.realpath(os.path.expanduser("~")), ".agent-pm/sessions/claude")


def reference(cwd: str, sid: str = SID) -> str:
    """ClaudeCommon.resume's text: no env value, only where its launch state is."""
    q = shlex.quote
    return f"cd {q(cwd)} && agent-pm-session client-resume claude {q(store())} {q(cwd)} {sid}"


def tui_launch(work, prompt="hi", config=CONFIG, *, resume=False, **spec) -> Launch:
    spec = Spec(**{"workdir": work, "sid": SID, "prompt": prompt, "resume": resume, **spec})
    return ClaudeAdapter(config).tui().launch(spec, HOOK)


def said(mid, *blocks) -> dict:
    """An assistant transcript record of message `mid` (None: no id)."""
    return {
        "type": "assistant",
        "message": {**({"id": mid} if mid else {}), "content": list(blocks)},
    }


LA = "America/Los_Angeles"


def epoch(stamp, zone=LA) -> float:
    return datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(zone)).timestamp()


NOW = epoch("2026-10-11 10:00")
