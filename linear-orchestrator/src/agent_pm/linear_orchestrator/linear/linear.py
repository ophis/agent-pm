"""Linear's GraphQL API as a given Keychain service (linear_gql), the Linear orchestrator's log
(log, into LOGS_DIR once an entry's main sets it), comments by client id and the checks of the
config's Linear ids.
"""

import datetime as dt
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import ConfigError, LinearError, Unavailable
from agent_pm.utils import append_line, printable

URL = "https://api.linear.app/graphql"
SECURITY = "/usr/bin/security"  # absolute: no PATH entry stands in for the Keychain
LOGS_DIR = None


def use_logs(path):
    global LOGS_DIR
    LOGS_DIR = path


def stamp():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def operation(query):
    m = re.match(r"[^{]*\{\s*([_A-Za-z]\w*)", query)
    return m[1] if m else "?"


def linear_gql(query, *, service, timeout=30, **variables):
    auth = key(service, timeout)
    req = urllib.request.Request(
        URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "Authorization": auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:  # a URLError: before it
        if e.code >= 500:
            reason = None
        elif e.code == 400 and _rate_limited(e):
            reason = "RATELIMITED"
        else:
            raise
        e.close()
        raise Unavailable(operation(query), status=e.code, reason=reason) from None
    except (OSError, http.client.HTTPException) as e:
        cause = e.reason if isinstance(e, urllib.error.URLError) else e
        raise Unavailable(operation(query), reason=one_line(cause)) from None
    try:
        body = json.loads(raw)
    except ValueError:
        raise Unavailable(operation(query), reason="response not JSON") from None
    if body.get("errors"):
        raise LinearError.api(body["errors"])
    return body["data"]


def _rate_limited(e):
    try:
        return any(
            (x.get("extensions") or {}).get("code") == "RATELIMITED"
            for x in json.loads(e.read())["errors"]
        )
    except Exception:
        return False


def key(service, timeout):
    found = subprocess.run(
        [SECURITY, "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    ).stdout.strip()
    if not _clean(found):
        raise LinearError.invalid_key(service)
    return found


def has_key(service):
    return (
        subprocess.run(
            [SECURITY, "find-generic-password", "-s", service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _clean(s):
    return bool(s) and s.isprintable() and not any(c.isspace() for c in s)


ISSUE_ID = r"[A-Z][A-Z0-9]*-\d+"
ONCE = ("src", "kind", "issue", "entry", "reason")
WINDOW = timedelta(hours=24)
CONFIG_ERRORS = (LinearError, ValueError, OSError)


def log(src, kind, issue=None, *, once=False, dry=False, **fields):
    line = {
        k: v
        for k, v in {"ts": stamp(), "src": src, "kind": kind, "issue": issue, **fields}.items()
        if v is not None
    }
    logs = LOGS_DIR
    path, key = os.path.join(logs, "orchestrator.jsonl") if logs else None, _key(line)
    if once and not dry and path and key in _recent_keys(path):
        return
    text = json.dumps(line, ensure_ascii=False)
    try:
        if not dry and path:
            os.makedirs(logs, exist_ok=True)
            append_line(path, text + "\n")
    except OSError:
        pass
    try:
        if dry or sys.stderr.isatty():
            print(printable(text), file=sys.stderr, flush=True)
    except OSError:  # a gone tmux pane's EIO
        pass


def config_error(src, e, dry=False):
    reason = one_line(str(e) if isinstance(e, (LinearError, ConfigError)) else e)
    return _stop(src, "config-error", reason, dry, reason=reason)


def linear_error(src, e, dry=False):
    return _stop(
        src,
        "linear-error",
        f"Linear unavailable: {e}",
        dry,
        op=e.op,
        status=e.status,
        reason=e.reason,
    )


def _stop(src, kind, message, dry, **fields):
    log(src, kind, once=True, dry=dry, **fields)
    try:
        if sys.stderr.isatty() and not dry:  # under a scheduler stderr is the log
            print(f"{src}.py: {message}", file=sys.stderr, flush=True)
    except OSError:
        pass
    return 1


def _key(line):
    return json.dumps([line.get(k) for k in ONCE])


def _recent_keys(path):
    cutoff, keys = dt.datetime.now(UTC) - WINDOW, set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                    if datetime.fromisoformat(line["ts"]) >= cutoff:
                        keys.add(_key(line))
                except (ValueError, TypeError, KeyError):
                    pass
    except OSError:
        pass
    return keys


def one_line(x):
    # other attributes, a process's output or the request, may hold the key
    text = f"{type(x).__name__}: {x}" if isinstance(x, BaseException) else str(x)
    return " ".join(text.split())


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


M_COMMENT = "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }"
M_COMMENT_ID = (
    "mutation($c: String!, $i: String!, $b: String!) { commentCreate(input: { id: $c, issueId: $i, body: $b }) "
    "{ success } }"
)
Q_COMMENT = "query($c: String!) { comment(id: $c) { id body issue { id } user { isMe } } }"
M_STATE = (
    "mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }"
)
M_SUBSCRIBE = (
    "mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }"
)
Q_ISSUE_STATE = "query($i: String!) { issue(id: $i) { state { id } } }"
# orderBy createdAt returns newest first, so the latest moves are on this page.
HISTORY = (
    "history(first: 250, orderBy: createdAt) { nodes { createdAt actorId fromStateId toStateId } }"
)
NOT_FOUND = "Entity not found"


def call(gql, query, field, **v):
    out = gql(query, **v)[field]
    if not out["success"]:
        raise LinearError.unsuccessful(field)
    return out


def client_id(*parts):
    return str(uuid.UUID(bytes=hashlib.sha256("/".join(parts).encode()).digest()[:16], version=4))


def not_found(e):
    return isinstance(e, LinearError) and NOT_FOUND in str(e)


def comment(gql, issue, body, cid=None):
    if cid is None:
        call(gql, M_COMMENT, "commentCreate", i=issue, b=body)
    else:
        call(gql, M_COMMENT_ID, "commentCreate", c=cid, i=issue, b=body)


def find_comment(gql, cid, issue_id):
    try:
        found = gql(Q_COMMENT, c=cid)["comment"]
    except LinearError as e:
        if not_found(e):
            return None
        raise
    if found is None:
        return None
    if (found.get("issue") or {}).get("id") != issue_id or not (found.get("user") or {}).get(
        "isMe"
    ):
        raise LinearError.foreign_comment(cid)
    return found["body"]


def subscribe(gql, issue, emails):
    notes = ""
    for email in emails:
        try:
            call(gql, M_SUBSCRIBE, "issueSubscribe", i=issue, e=email)
        except (Exception, LinearError) as e:
            notes += f"\n\nCould not subscribe {email}: {one_line(e)}"
    return notes


def move(gql, issue, to, frm):
    now = gql(Q_ISSUE_STATE, i=issue)["issue"]["state"]["id"]
    if now != frm:
        return now
    call(gql, M_STATE, "issueUpdate", i=issue, s=to)
    return None


def comment_and_move(gql, issue, body, to, frm, emails=()):
    # move first: a failed or skipped move posts no comment, so a retry never repeats it
    notes = subscribe(gql, issue, emails)
    left = move(gql, issue, to, frm)
    if left is None:
        comment(gql, issue, body + notes)
    return left


def last_move(nodes, states, actors=None):
    return max(
        (
            parse_time(n["createdAt"])
            for n in nodes
            if n["toStateId"] in states and (actors is None or n["actorId"] in actors)
        ),
        default=None,
    )


Q_USER = "query($e: String!) { users(filter: { email: { eqIgnoreCase: $e } }) { nodes { id } } }"


def user_id(gql, email):
    nodes = gql(Q_USER, e=email)["users"]["nodes"]
    return nodes[0]["id"] if nodes else None


def humans(gql, cfg):
    emails = cfg.human_members
    ids = [user_id(gql, e) for e in emails]
    if missing := [e for e, i in zip(emails, ids, strict=False) if not i]:
        raise LinearError.human_members_not_found_in_linear(", ".join(missing))
    return ids


def role_ids(gql, roles):
    out = {}
    for name, role in roles.items():
        uid = user_id(gql, role.account)
        if not uid:
            raise LinearError.roles_account_not_found_in_linear(name, role.account)
        out[uid] = name
    return out


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    states: dict


Q_TEAM = """query($t: ID) { teams(filter: { id: { eq: $t } }) { nodes { id name
  states(first: 100) { nodes { id } } } } }"""


def team(gql, cfg):
    nodes = gql(Q_TEAM, t=cfg.team)["teams"]["nodes"]
    if not nodes:
        raise LinearError.team_not_found_in_linear(cfg.team)
    t = nodes[0]
    ids = {s["id"] for s in t["states"]["nodes"]}
    if bad := [f"{k} {cfg.states[k]}" for k in config.STATES if cfg.states[k] not in ids]:
        raise LinearError.states_not_workflow_states_of_team(t["name"], ", ".join(bad))
    return Team(t["id"], t["name"], {k: cfg.states[k] for k in config.STATES})


Q_TASK_GROUP = (
    "query($i: String!) { issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } } }"
)


def task_group(gql, cfg):
    # children are unpaginated (250): beyond that a valid id fails
    group = cfg.task_label_group
    try:
        label = gql(Q_TASK_GROUP, i=group)["issueLabel"]
    except LinearError as e:
        raise LinearError.task_label_group_not_found_in_linear(group, e) from None
    if not label["isGroup"]:
        raise LinearError.task_label_group_is_not_a_label(group)
    ids = {c["id"] for c in label["children"]["nodes"]}
    if bad := [f"{task} {i}" for task, i in cfg.task_labels.items() if i not in ids]:
        raise LinearError.task_labels_not_labels_of_task_label(group, ", ".join(bad))
