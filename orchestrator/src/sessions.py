import functools
import os
import re
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
from clients import claude as claude_client  # noqa: E402
import drive  # noqa: E402
import linear  # noqa: E402
from linear import one_line  # noqa: E402

URL = "https://agent-pm.invalid/run/"
LIMIT = 10
Q_FIND = ("query($i: String!, $p: String!) { issue(id: $i) { comments(filter: { user: { isMe: { eq: true } }, "
          "body: { startsWith: $p } }) { nodes { id createdAt } } } }")
M_UPDATE = "mutation($c: String!, $b: String!) { commentUpdate(id: $c, input: { body: $b }) { success } }"


def is_record(attachment):
    # the URL alone identifies one: a source may be titled "Run rate analysis"
    return attachment["url"].startswith(URL)


def is_comment(comment):
    # isMe means the harness account because callers query with its key (linear.linear_gql)
    return bool((comment["user"] or {}).get("isMe")) and re.match(f"Run {config.UUID_RE.pattern} · ", comment["body"]) is not None


def base(*, sid, workdir, started_at):
    return {"sid": sid, "workdir": workdir, "started_at": started_at}


def command(rec):
    # built here, never read from the record: the agent run can write it
    entry = drive.session(rec["workdir"], rec["sid"])
    return claude_client.resume(entry["cwd"] if entry else rec["workdir"], rec["sid"], rec["workdir"])


def prefix(sid):
    return f"Run {sid} · "


def body(rec, rc=None, ended_at=None, *, outcome=None):
    if rc is None:
        line = f"running · {rec['started_at']}"
    else:
        status = outcome.status if outcome is not None else "done" if rc == 0 else "interrupted"
        line = f"{status} · {rec['started_at']} → {ended_at} · exit {rc}"
    return f"{prefix(rec['sid'])}{line}\n\n```\n{command(rec)}\n```"


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write(gql, issue, sid, text, limit=LIMIT):
    # the thread bounds both calls, key reads included
    out = {}

    def call():
        try:
            found = gql(Q_FIND, i=issue, p=prefix(sid))["issue"]["comments"]["nodes"]
            if found:
                earliest = min(found, key=lambda c: c["createdAt"])  # uniform UTC ISO strings sort by time
                out["ok"] = gql(M_UPDATE, c=earliest["id"], b=text)["commentUpdate"]["success"]
            else:
                out["ok"] = gql(linear.M_COMMENT, i=issue, b=text)["commentCreate"]["success"]
        except (Exception, SystemExit) as e:
            out["error"] = e

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        return f"timed out after {limit}s"
    if "error" in out:
        return one_line(out["error"])
    return None if out["ok"] else "success: false"


def post(issue, rec, rc=None, *, gql=None, ended_at=None, outcome=None):
    text = body(rec) if rc is None else body(rec, rc, ended_at or now(), outcome=outcome)
    return write(gql or functools.partial(linear.linear_gql, timeout=LIMIT), issue, rec["sid"], text)
