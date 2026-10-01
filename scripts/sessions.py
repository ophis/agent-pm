#!/usr/bin/env python3
"""Session registry: one Linear attachment `Run <sid>` per Claude session on its issue, written by the harness account.

sessions.py start ISSUE RECORD_JSON      record the session as running (before claude starts)
sessions.py end ISSUE RECORD_JSON RC     record its end: done for RC 0, else interrupted
RECORD_JSON is base()'s record, built by launch.py. One write attempt, bounded by LIMIT seconds; a failure prints
one registry-error line. Always exits 0.
"""
import functools
import json
import os
import shlex
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline  # noqa: E402

URL = "https://agent-pm.invalid/run/"  # .invalid never resolves, so a click sends the sid nowhere
LIMIT = 10
M_ATTACH = "mutation($in: AttachmentCreateInput!) { attachmentCreate(input: $in) { success } }"
USAGE = "usage: sessions.py start ISSUE RECORD_JSON | end ISSUE RECORD_JSON RC"


def url(sid):
    return URL + sid


def is_record(attachment):
    """True for a session record. The URL alone identifies one: a source may be titled "Run rate analysis"."""
    return attachment["url"].startswith(URL)


def base(*, sid, cwd, role, task, key, model, started_at, repo=None, branch=None, worktree=None):
    """The record without status. key is the role's Keychain service name, never a secret; repo is "<owner>/<name>",
    given with branch and worktree for an Engineering run only."""
    q = shlex.quote
    rec = {"sid": sid, "cwd": cwd, "role": role, "task": task, "started_at": started_at,
           "resume_command": f"cd {q(cwd)} && LINEAR_KEYCHAIN_SERVICE={q(key)} claude --resume {q(sid)}", "model": model}
    if repo is not None:
        rec.update(repo=repo, branch=branch, worktree=worktree)
    return rec


def started(rec):
    rec = {k: v for k, v in rec.items() if k not in ("ended_at", "exit")}
    return {**rec, "status": "running"}


def ended(rec, rc, now):
    return {**rec, "status": "done" if rc == 0 else "interrupted", "ended_at": now, "exit": rc}


def attachment(issue, rec):
    """The AttachmentCreateInput. Linear upserts it by (issueId, url) and replaces metadata wholesale."""
    times = f"{rec['started_at']} → {rec['ended_at']} · exit {rec['exit']}" if "ended_at" in rec else rec["started_at"]
    return {"issueId": issue, "url": url(rec["sid"]), "title": f"Run {rec['sid']}",
            "subtitle": f"{rec['status']} · {times} · {rec['resume_command']}", "metadata": rec}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def one_line(e):
    """type and message only: other attributes (a process's output, the request) may hold the key."""
    return " ".join(f"{type(e).__name__}: {e}".split())


def write(gql, issue, rec, limit=LIMIT):
    """None once Linear stored rec, else a one-line reason; never raises. The thread bounds key read and request."""
    out = {}

    def call():
        try:
            out["ok"] = gql(M_ATTACH, **{"in": attachment(issue, rec)})["attachmentCreate"]["success"]
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
        reason = write(gql, issue, started(rec) if cmd == "start" else ended(rec, int(argv[3]), now()))
    except Exception as e:
        reason = one_line(e)
    if reason:
        print(" ".join(f"{datetime.now():%Y-%m-%d %H:%M:%S} registry-error {issue} session={sid}: {reason}".split()),
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
