"""Session comments: an agent run's one Linear comment (`Run <sid> · …`), posted by the harness account."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

import linear  # noqa: E402

URL = "https://agent-pm.invalid/run/"
LIMIT = 10
now = linear.stamp


def is_record(attachment):
    # the URL alone identifies one: a source may be titled "Run rate analysis"
    return attachment["url"].startswith(URL)


def is_comment(comment):
    # isMe means the harness account because callers query with its key
    return (
        bool((comment["user"] or {}).get("isMe"))
        and re.match(f"Run {config.UUID_RE.pattern} · ", comment["body"]) is not None
    )
