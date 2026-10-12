"""The runs log (runs.jsonl): one durable JSON line per run event."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402, F401

import linear  # noqa: E402
from utils.files import append_line  # noqa: E402


def note(path, kind, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({"ts": linear.stamp(), "kind": kind, **fields}, ensure_ascii=False)
    append_line(path, line + "\n", durable=True)
