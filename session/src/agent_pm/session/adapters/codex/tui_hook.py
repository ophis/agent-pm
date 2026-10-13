"""Codex binding callback path-loaded by the Tui mode hook running with Python -I."""

from __future__ import annotations

import sys

from agent_pm.session.adapters.codex import sessions
from agent_pm.session.adapters.codex.hook_schema import HOOKS

LABELS = {label for label, _, _ in HOOKS}


def bind(label, value, workdir, sid, *, root=None):
    if (
        label not in LABELS
        or value.get("hook_event_name", label) != label
        or not sessions.hook_identity(value)
    ):
        return False
    try:
        store = sessions.SessionStore(root)
        state = store.load(workdir, sid)
        if state is None:
            raise sessions.RecoveryError("missing Codex recovery state")
        if not sessions.hook_cwd_matches(value, state.snapshot["cwd"]):
            return False
        store.bind(workdir, sid, value["session_id"], timeout=1 if label == "Interrupt" else 5)
    except (sessions.RecoveryError, OSError, ValueError) as exc:
        print(f"codex hook: {exc}", file=sys.stderr)
        return False
    return True


def on_hook(label, payload, state):
    if not bind(
        label,
        payload,
        state["workdir"],
        state["sid"],
        root=state["adapter_state"]["store"],
    ):
        return False
    payload["hook_event_name"] = label
    return True
