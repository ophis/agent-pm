"""Session registry: one comment per Claude session on its issue, written by the harness account.

post() finds the harness account's comments starting `Run <sid> · ` and updates the earliest, else creates one:
one attempt, bounded by LIMIT seconds.
"""
import functools
import os
import re
import shlex
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import linear  # noqa: E402
from linear import one_line  # noqa: E402

URL = "https://agent-pm.invalid/run/"
LIMIT = 10
Q_FIND = ("query($i: String!, $p: String!) { issue(id: $i) { comments(filter: { user: { isMe: { eq: true } }, "
          "body: { startsWith: $p } }) { nodes { id createdAt } } } }")
M_CREATE = "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }"
M_UPDATE = "mutation($c: String!, $b: String!) { commentUpdate(id: $c, input: { body: $b }) { success } }"


def is_record(attachment):
    """True for an old attachment record. The URL alone identifies one: a source may be titled "Run rate analysis"."""
    return attachment["url"].startswith(URL)


def is_comment(comment):
    """True for a session comment: by the harness account and starting `Run <sid> · `. isMe means the harness account
    because callers query with its key (linear.linear_gql)."""
    return bool((comment["user"] or {}).get("isMe")) and re.match(f"Run {config.UUID_RE.pattern} · ", comment["body"]) is not None


def base(*, sid, cwd, started_at):
    return {"sid": sid, "cwd": cwd, "started_at": started_at}


def command(rec):
    q = shlex.quote
    return f"cd {q(rec['cwd'])} && claude --resume {q(rec['sid'])}"


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


def post(issue, rec, rc=None, *, gql=None, ended_at=None):
    """Record the session rec on issue: running for rc None, else its end (ended_at defaults to now). gql defaults to
    the harness account's, bounded by LIMIT. None once written, else the one unstamped registry-error line."""
    sid = rec["sid"]
    text = body(rec) if rc is None else body(rec, rc, ended_at or now())
    reason = write(gql or functools.partial(linear.linear_gql, timeout=LIMIT), issue, sid, text)
    if reason:
        return one_line(f"registry-error {issue} session={sid}: {reason}")
    return None
