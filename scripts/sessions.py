#!/usr/bin/env python3
"""Session registry: one comment per Claude session on its issue, written by the harness account.

sessions.py start ISSUE RECORD_JSON      record the session as running (before claude starts)
sessions.py end ISSUE RECORD_JSON RC     record its end: done for RC 0, else interrupted
RECORD_JSON is base()'s record, built by launch.py. Each write finds the harness account's comments starting
`Run <sid> · ` and updates the earliest, else creates one: one attempt, bounded by LIMIT seconds; a failure prints one
registry-error line. Always exits 0.
"""
import functools
import json
import os
import re
import shlex
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline  # noqa: E402

SID = r"[0-9a-f-]{36}"
URL = "https://agent-pm.invalid/run/"
LIMIT = 10
Q_FIND = ("query($i: String!, $p: String!) { issue(id: $i) { comments(filter: { user: { isMe: { eq: true } }, "
          "body: { startsWith: $p } }) { nodes { id createdAt } } } }")
M_CREATE = "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }"
M_UPDATE = "mutation($c: String!, $b: String!) { commentUpdate(id: $c, input: { body: $b }) { success } }"
USAGE = "usage: sessions.py start ISSUE RECORD_JSON | end ISSUE RECORD_JSON RC"


def is_record(attachment):
    """True for an old attachment record. The URL alone identifies one: a source may be titled "Run rate analysis"."""
    return attachment["url"].startswith(URL)


def is_comment(comment):
    """True for a session comment: by the harness account and starting `Run <sid> · `. isMe means the harness account
    because callers query with its key (pipeline.linear_gql)."""
    return bool((comment["user"] or {}).get("isMe")) and re.match(f"Run {SID} · ", comment["body"]) is not None


def base(*, sid, cwd, key, started_at):
    """The record. key is the role's Keychain service name, never a secret."""
    return {"sid": sid, "cwd": cwd, "key": key, "started_at": started_at}


def command(rec):
    q = shlex.quote
    return f"cd {q(rec['cwd'])} && LINEAR_KEYCHAIN_SERVICE={q(rec['key'])} claude --resume {q(rec['sid'])}"


def prefix(sid):
    return f"Run {sid} · "


def body(rec, rc=None, ended_at=None):
    """The comment: a status line, then the resume command in a code block."""
    if rc is None:
        line = f"running · {rec['started_at']}"
    else:
        line = f"{'done' if rc == 0 else 'interrupted'} · {rec['started_at']} → {ended_at} · exit {rc}"
    return f"{prefix(rec['sid'])}{line}\n\n```\n{command(rec)}\n```"


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def one_line(e):
    """type and message only: other attributes (a process's output, the request) may hold the key."""
    return " ".join(f"{type(e).__name__}: {e}".split())


def write(gql, issue, sid, text, limit=LIMIT):
    """None once text is sid's comment on issue, else a one-line reason; never raises. A failed find writes nothing.
    The thread bounds both calls, key reads included."""
    out = {}

    def call():
        try:
            found = gql(Q_FIND, i=issue, p=prefix(sid))["issue"]["comments"]["nodes"]
            if found:
                earliest = min(found, key=lambda c: c["createdAt"])  # uniform UTC ISO strings sort by time
                out["ok"] = gql(M_UPDATE, c=earliest["id"], b=text)["commentUpdate"]["success"]
            else:
                out["ok"] = gql(M_CREATE, i=issue, b=text)["commentCreate"]["success"]
        except (Exception, SystemExit) as e:  # SystemExit: linear_gql's API error
            out["error"] = e

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        return f"timed out after {limit}s"
    if "error" in out:
        return one_line(out["error"])
    return None if out["ok"] else "success: false"


def main(argv, gql=None):
    gql = gql or functools.partial(pipeline.linear_gql, timeout=LIMIT)
    issue, sid = argv[1] if len(argv) > 1 else "?", "?"
    try:
        cmd = argv[0] if argv else ""
        if (cmd, len(argv)) not in (("start", 3), ("end", 4)):
            raise ValueError(USAGE)
        rec = json.loads(argv[2])
        if not isinstance(rec, dict):
            raise ValueError("RECORD_JSON is not an object")
        sid = rec.get("sid", "?")
        reason = write(gql, issue, sid, body(rec) if cmd == "start" else body(rec, int(argv[3]), now()))
    except Exception as e:
        reason = one_line(e)
    if reason:
        print(" ".join(f"{datetime.now():%Y-%m-%d %H:%M:%S} registry-error {issue} session={sid}: {reason}".split()),
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
