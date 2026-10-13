"""Codex binding callback loaded by the standalone Driver hook."""

from __future__ import annotations

import importlib.util
import os
import sys

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


def _sibling(name):
    key, path = "_agent_pm_codex_" + name, os.path.join(DIRECTORY, name + ".py")
    if key in sys.modules:
        found = getattr(sys.modules[key], "__file__", None)
        if found != path:
            raise ImportError(f"{key} is {found}, not {path}")
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    # registered before it runs: dataclasses with postponed annotations look up their module
    module = sys.modules[key] = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[key]
        raise
    return module


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
        return True
    except (sessions.RecoveryError, OSError, ValueError) as exc:
        print(f"codex hook: {exc}", file=sys.stderr)
        return False


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
