import re
from datetime import UTC, datetime

from agent_pm.delegate import Outcome, Result
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.unit.run.support import HARNESS, ID, SESSION, SID

RUNTIME = "claude"

PR = "https://github.com/Ophis/Agent-PM/pull/7"

TITLE = "TASK-7: Session registry"

DONE = Outcome("done", TITLE, "Opened the PR.", url=PR)

DONE_RESULT = Result(0, DONE)

NOW = "2026-10-04T10:00:00+08:00"

CMD = f"resume {SID}"

TUI = f"engineer-{ID}-{SID[:8]}"

MATCH = re.escape(f"engineer-{ID}-") + ".*"

T = datetime(2026, 10, 11, 10, 0, tzinfo=UTC).timestamp()

NO_OUTCOME = Result(0, None, "the agent run returned no outcome")

RUNNING = f"running · #1 · {NOW}"

REPAIR = ("find", HARNESS, {"c": SESSION})


def text(line):
    return f"Run {SID} · {line}\n\n```\n{CMD}\n```"


def ended(status, rc, seq=2):
    return f"{status} · #{seq} · {NOW} → {NOW} · exit {rc}"


def created(line):
    return [
        ("update", HARNESS, {"c": SESSION, "b": text(line)}),
        ("comment", HARNESS, {"c": SESSION, "i": ID, "b": text(line)}),
    ]


def updated(line):
    return [("update", HARNESS, {"c": SESSION, "b": text(line)})]


def cid(step):
    return linear.client_id(SID, step)
