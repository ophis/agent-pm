"""Codex binding callback loaded by the standalone Driver hook."""

from __future__ import annotations

import runpy
import os
import sys

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


_sibling = runpy.run_path(os.path.join(DIRECTORY, "siblings.py"))["load"]

sessions = _sibling("sessions")
HOOKS = _sibling("hook_schema").HOOKS


LABELS = {label for label, _, _ in HOOKS}


def bind(label, value, workdir, sid, *, root=None):
    if (
        label not in LABELS
        or value.get("hook_event_name", label) != label
        or value.get("agent_id") is not None
        or not sessions.native_id(value.get("session_id"))
        or not isinstance(value.get("cwd"), str)
        or not os.path.isabs(value["cwd"])
        or "\0" in value["cwd"]
    ):
        return False
    try:
        state = sessions.load_session(workdir, sid, root=root)
        if state is None:
            raise sessions.RecoveryError("missing Codex recovery state")
        if os.path.realpath(value["cwd"]) != state.snapshot["cwd"]:
            return False
        sessions.bind_session(
            workdir, sid, value["session_id"], timeout=1 if label == "Interrupt" else 5, root=root
        )
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
