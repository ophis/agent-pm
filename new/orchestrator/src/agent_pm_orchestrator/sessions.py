"""Session comments: an agent run's one Linear comment (`Run <sid> · …`), posted by the harness account under its
session id (`client_id(sid, "session")`), numbered `#<seq>`; repair rewrites it when a late write left an older one."""

import re
import threading

from agent_pm.driver import api

from agent_pm_orchestrator import config, linear

URL = "https://agent-pm.invalid/run/"
LIMIT = 10
M_UPDATE = (
    "mutation($c: String!, $b: String!) { commentUpdate(id: $c, input: { body: $b }) { success } }"
)
SEQ = re.compile(r" · #(\d+) · (\S+)")
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


def base(*, sid, workdir, runtime, started_at):
    return {"sid": sid, "workdir": workdir, "runtime": runtime, "started_at": started_at, "seq": 0}


def command(rec):
    # only cwd is read from the session entry: the agent run can write run.jsonl
    try:
        entry = api.session(rec["workdir"], rec["sid"])
        cwd = entry["cwd"] if entry else rec["workdir"]
        st = api.session_info(rec["runtime"], rec["sid"], cwd)
    except Exception:
        return None
    return st.resume_command if st.resumable != "unknown" and st.resume_command else None


def _status(rc, outcome, limit):
    if outcome is not None:
        return outcome.status
    if limit:
        return "limit"
    return "done" if rc == 0 else "interrupted"


def body(rec, rc=None, ended_at=None, *, outcome=None, limit=False):
    rec["seq"] += 1
    if rc is None:
        line = f"running · #{rec['seq']} · {rec['started_at']}"
    else:
        status = _status(rc, outcome, limit)
        line = f"{status} · #{rec['seq']} · {rec['started_at']} → {ended_at} · exit {rc}"
    cmd = command(rec)
    return f"Run {rec['sid']} · {line}" + ("" if cmd is None else f"\n\n```\n{cmd}\n```")


def _bounded(call, limit):
    # the thread bounds every call, key reads included
    out = {}

    def run():
        try:
            out["value"] = call()
        except (Exception, SystemExit) as e:
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        return None, f"timed out after {limit}s"
    if "error" in out:
        return None, linear.one_line(out["error"])
    return out["value"], None


def write(gql, sid, issue, text, limit=LIMIT):
    cid = linear.client_id(sid, "session")

    def call():
        try:
            done = gql(M_UPDATE, c=cid, b=text)["commentUpdate"]
        except SystemExit as e:
            if not linear.not_found(e):
                raise
            done = None
        if done is None:
            done = gql(linear.M_COMMENT_ID, c=cid, i=issue, b=text)["commentCreate"]
        return done["success"]

    ok, error = _bounded(call, limit)
    return error or (None if ok else "success: false")


def _text(rec):
    rc, ended_at, outcome, limit = rec["args"]
    out = body(rec, rc, ended_at, outcome=outcome, limit=limit)
    rec["last"] = rec["seq"]
    return out


def post(issue, rec, rc=None, *, gql, ended_at=None, outcome=None, limit=False):
    rec["args"] = (rc, None if rc is None else ended_at or now(), outcome, limit)
    return write(gql, rec["sid"], issue, _text(rec))


def repair(issue, issue_id, rec, *, gql, limit=LIMIT):
    found, error = _bounded(
        lambda: linear.find_comment(gql, linear.client_id(rec["sid"], "session"), issue_id), limit
    )
    if error:
        return error
    seq = found and SEQ.search(found.split("\n", 1)[0])
    if seq and int(seq[1]) >= rec["last"] and seq[2] == rec["started_at"]:
        return None
    return write(gql, rec["sid"], issue, _text(rec), limit=limit)
