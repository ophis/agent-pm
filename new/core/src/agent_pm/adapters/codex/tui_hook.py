from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sessions
from hook_schema import HOOKS

LABELS = {label for label, _, _ in HOOKS}
PAYLOAD_MAX = 64 << 20
BLOCK = 1 << 16


def payload(stream):
    chunks, size = [], 0
    while chunk := stream.read(BLOCK):
        size += len(chunk)
        if size <= PAYLOAD_MAX:
            chunks.append(chunk)
    if size > PAYLOAD_MAX:
        return {}
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


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


def forward(hook, label, value):
    with tempfile.TemporaryFile() as stream:
        stream.write(
            json.dumps({**value, "hook_event_name": label}, ensure_ascii=False).encode("utf-8")
        )
        stream.seek(0)
        os.dup2(stream.fileno(), 0)
        os.execv(hook[0], [*hook, label])


def main(argv):
    value = payload(sys.stdin.buffer)
    if len(argv) != 5:
        print("codex hook: expected WORKDIR SID STORE HOOK_JSON LABEL", file=sys.stderr)
        return
    workdir, sid, root, encoded, label = argv
    try:
        hook = json.loads(encoded)
    except (ValueError, RecursionError):
        return
    if (
        not isinstance(hook, list)
        or not hook
        or not all(isinstance(w, str) and "\0" not in w for w in hook)
        or not os.path.isabs(hook[0])
    ):
        return
    if bind(label, value, workdir, sid, root=root):
        forward(hook, label, value)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print(f"codex hook: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(0)
