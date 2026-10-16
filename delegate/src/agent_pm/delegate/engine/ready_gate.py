"""Readies a done pull-request run's PR only when G1 its autopilot converged, G2 at the PR head, G3
the head's checks pass; else the run fails and the PR stays draft. Gotcha: owner/name are the
agent's, so it may name another PR on an allowed host.
"""

import json
import subprocess
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlsplit

from agent_pm.delegate.engine.types import Gated, GateStatus
from agent_pm.delegate.event.outcome import PULL, summary_max
from agent_pm.delegate.event.types import Autopilot, AutopilotStatus, Outcome, OutcomeStatus
from agent_pm.utils import SHORT, Runner, err_text, one_line, printable, sh

REASON_MAX = 300


class _Check(StrEnum):
    PASS = "pass"
    SKIPPED = "skipped"
    FAIL = "fail"


class _GhError(Exception):
    pass


@dataclass(frozen=True)
class _View:
    state: str
    draft: bool
    head: str
    checks: list[object]


def gate(outcome: Outcome, autopilot: Autopilot | None, host: str, *, run: Runner = sh) -> Gated:
    """`host`: the run's output host."""
    url, done = outcome.url, outcome.status == OutcomeStatus.DONE
    parts = urlsplit(url)
    if not PULL.fullmatch(parts.path):
        return Gated(outcome, GateStatus.SKIPPED, "")
    if parts.netloc != host:
        return _refuse(outcome, f"url host {parts.netloc} is not {host}", done=done)
    try:
        view = _view(url, run)
    except _GhError as e:
        return _refuse(outcome, str(e), done=done)
    if not done:
        status, error = _undo(url, view, run)
        return Gated(outcome, status, _line(error))
    reason = _judge(autopilot, view)
    if reason is not None:
        _, error = _undo(url, view, run)
        return _failed(outcome, f"{reason}; {error}" if error else reason)
    if view.draft:
        try:
            _gh(run, "gh pr ready", ["pr", "ready", url])
        except _GhError as e:
            return _failed(outcome, str(e))
    return Gated(outcome, GateStatus.PASSED, "")


def _refuse(outcome: Outcome, reason: str, *, done: bool) -> Gated:
    if done:
        return _failed(outcome, reason)
    return Gated(outcome, GateStatus.KEPT, _line(reason))


def _failed(outcome: Outcome, reason: str) -> Gated:
    reason = _line(reason)
    line = f"ready gate: {reason}"
    kept = outcome.summary[: summary_max() - len(line) - 1].rstrip()
    summary = f"{kept}\n{line}" if kept else line
    return Gated(
        replace(outcome, status=OutcomeStatus.FAILED, summary=summary), GateStatus.FAILED, reason
    )


def _line(text: str) -> str:
    return printable(one_line(text))[:REASON_MAX]


def _gh(run: Runner, label: str, argv: list[str]) -> str:
    try:
        res = run(["gh", *argv], SHORT)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise _GhError(f"{label}: {one_line(e)}") from None
    if res.returncode != 0:
        raise _GhError(f"{label}: {err_text(res)}")
    return res.stdout


def _view(url: str, run: Runner) -> _View:
    out = _gh(
        run,
        "gh pr view",
        ["pr", "view", url, "--json", "state,isDraft,headRefOid,statusCheckRollup"],
    )
    try:
        data = json.loads(out)
    except ValueError as e:
        raise _GhError(f"gh pr view: {e}") from None
    if not isinstance(data, dict):
        raise _GhError("gh pr view: not an object")
    state, draft, head = data.get("state"), data.get("isDraft"), data.get("headRefOid")
    checks = data.get("statusCheckRollup")
    if (
        not isinstance(state, str)
        or not isinstance(draft, bool)
        or not isinstance(head, str)
        or not isinstance(checks, list | None)
    ):
        raise _GhError("gh pr view: unexpected state, isDraft, headRefOid or statusCheckRollup")
    return _View(state, draft, head, checks or [])


def _undo(url: str, view: _View, run: Runner) -> tuple[GateStatus, str]:
    """Drafts an open ready PR: (KEPT, "") nothing to do, (DRAFTED, "") drafted, (KEPT, error)."""
    if view.state != "OPEN" or view.draft:
        return GateStatus.KEPT, ""
    try:
        _gh(run, "gh pr ready --undo", ["pr", "ready", url, "--undo"])
    except _GhError as e:
        return GateStatus.KEPT, str(e)
    return GateStatus.DRAFTED, ""


def _judge(autopilot: Autopilot | None, view: _View) -> str | None:
    """The first of G1–G3 that fails, as a reason; None if all pass."""
    if autopilot is None:
        return "G1: no autopilot result"
    if autopilot.status != AutopilotStatus.CONVERGED:
        return f"G1: autopilot {autopilot.status}"
    if view.state != "OPEN":
        return f"G2: PR is {view.state}"
    if autopilot.head != view.head:
        return f"G2: autopilot head {autopilot.head[:12]} is not PR head {view.head[:12]}"
    checks = [_check(item) for item in view.checks]
    for name, kind, said in checks:
        if kind == _Check.FAIL:
            return f"G3: check {name} {said}"
    if checks and all(kind == _Check.SKIPPED for _, kind, _ in checks):
        return f"G3: CI did not run (CI 未运行): all {len(checks)} checks skipped"
    return None


def _check(item: object) -> tuple[str, _Check, str]:
    """(name, kind, GitHub's word) of a statusCheckRollup entry; unknown fails."""
    if not isinstance(item, dict):
        return "?", _Check.FAIL, "unreadable"
    name = str(item.get("name") or item.get("context") or "?")
    kind = item.get("__typename")
    if kind == "CheckRun":
        status, conclusion = str(item.get("status")), str(item.get("conclusion"))
        if status != "COMPLETED":
            return name, _Check.FAIL, status
        found = {"SUCCESS": _Check.PASS, "NEUTRAL": _Check.PASS, "SKIPPED": _Check.SKIPPED}
        return name, found.get(conclusion, _Check.FAIL), conclusion
    if kind == "StatusContext":
        state = str(item.get("state"))
        return name, _Check.PASS if state == "SUCCESS" else _Check.FAIL, state
    return name, _Check.FAIL, f"of unknown type {kind}"
