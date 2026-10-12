"""The runs log (runs.jsonl): one durable JSON line per run event."""

import json
import os

from agent_pm.utils.files import append_line

from agent_pm_orchestrator import linear


def note(path, kind, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({"ts": linear.stamp(), "kind": kind, **fields}, ensure_ascii=False)
    append_line(path, line + "\n", durable=True)
