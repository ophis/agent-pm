import re
from dataclasses import dataclass, field
from typing import NamedTuple

from agent_pm.linear_orchestrator.engine.routing import types as routing
from orchestrator_tests.integration.flows.fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    PRD_DIR,
    RESEARCH_DIR,
)

ROLE_OF = {a: r for r, a in ACCOUNTS.items()}


DIRS = {"researcher": RESEARCH_DIR, "pm": PRD_DIR}


TEXT_KINDS = {"done": "done", "needs_input": "question", "failed": "failed"}


RUN_STEPS = {
    "done": "run: in review",
    "needs_input": "run: needs input",
    "failed": "run: failed",
    "none": "run: no outcome",
}


RUNTIME = "claude"


RUN_KEYS = ("kind", "issue", "sid", "role", "runtime")


SESSION = re.compile(r"(running|done|failed|needs_input|interrupted|limit) · ")


FIRST_WRITE = ("sessions.M_UPDATE", HARNESS_EMAIL, "error")


HARNESS_TEXTS = {routing.CAP_COMMENT: "cap", routing.INTERRUPTED: "interrupted"}


RESEARCH = (
    "Session registries in agent harnesses",
    "How do agent harnesses keep track of their sessions?",
)


INSTRUCTIONS = "Write the PRD for a session registry."


REPORT = (
    "# Session registries in agent harnesses\n\nEach harness names a tmux session after its run."
)


BUILD = "Build the session registry."


def lookup(account):
    """A resumed run's look for an unledgered comment step's comment (FR-54): not found, then
    posted.
    """
    return ("linear.Q_COMMENT", account, "error")


def agent_calls(calls):
    """Agent runs carry --session-id or --resume; usage probes carry neither."""
    return [c for c in calls if {"--session-id", "--resume"} & set(c["argv"])]


@dataclass
class Want:
    state: str
    title: str
    history: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)
    ledger: dict = field(default_factory=dict)


class Script(NamedTuple):
    outcome: str
    steps: list
    report: dict | None
    texts: dict


class Pending(NamedTuple):
    ident: str
    role: str
    sid: str
    script: Script
    gate: str
    resumed: bool


@dataclass(frozen=True, kw_only=True)
class BoardChanges:
    state: str | None = None
    title: str | None = None
    history: tuple = ()
    comments: tuple = ()
    attachments: tuple = ()
    subscribers: tuple = ()
    ledger: dict | None = None
