"""Linear access: Keychain-keyed GraphQL transport, lookups of the config's users, team and task labels, the shared writes
and history, and small shared helpers.
Imports config, which puts core/src on sys.path.
"""
import functools
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime

import config  # noqa: E402
import drive  # noqa: E402


@functools.cache
def harness_service():
    """Keychain service of the harness account's Linear key: orchestrator/config.toml's harness_key."""
    return config.load_config()["harness_key"]


def linear_gql(query, *, timeout=30, service=None, **variables):
    """Linear as the account whose key is Keychain item `service`, default the harness account's."""
    key = subprocess.run(["security", "find-generic-password", "-s", service or harness_service(), "-w"],
                         capture_output=True, text=True, check=True, timeout=timeout).stdout.strip()
    req = urllib.request.Request("https://api.linear.app/graphql",
                                 data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"linear api error: {body['errors']}")
    return body["data"]


STAMP = "%Y-%m-%d %H:%M:%S"
ISSUE_ID = r"[A-Z][A-Z0-9]*-\d+"


def stamp():
    """Now, local, in STAMP."""
    return datetime.now().strftime(STAMP)


def append(path, line):
    """Appends `<stamp> <line>` to path (its directory created) and returns that line; raises OSError."""
    text = f"{stamp()} {line}".encode("utf-8", "replace").decode()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    drive.append_line(path, text + "\n")
    return text


def log(msg):
    print(f"{stamp()} {msg}", file=sys.stderr, flush=True)


def one_line(x):
    """An exception as `<Type>: <message>` (other attributes, a process's output or the request, may hold the key), anything else as str; whitespace collapsed."""
    text = f"{type(x).__name__}: {x}" if isinstance(x, BaseException) else str(x)
    return " ".join(text.split())


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


M_COMMENT = "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }"
M_STATE = "mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }"
M_SUBSCRIBE = "mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }"
Q_ISSUE_STATE = "query($i: String!) { issue(id: $i) { state { id } } }"
# orderBy createdAt returns newest first, so the latest moves are on this page.
HISTORY = "history(first: 250, orderBy: createdAt) { nodes { createdAt actorId fromStateId toStateId } }"


def call(gql, query, field, **v):
    """The result's field; RuntimeError when its success is false."""
    out = gql(query, **v)[field]
    if not out["success"]:
        raise RuntimeError(f"{field}: success: false")
    return out


def comment(gql, issue, body):
    call(gql, M_COMMENT, "commentCreate", i=issue, b=body)


def subscribe(gql, issue, emails):
    """Subscribes each email; returns the failures as notes for a comment. A signal SystemExit (an int code: a runner's
    handler) re-raises; linear_gql's (a str code) is an API error."""
    notes = ""
    for email in emails:
        try:
            call(gql, M_SUBSCRIBE, "issueSubscribe", i=issue, e=email)
        except (Exception, SystemExit) as e:
            if isinstance(e, SystemExit) and isinstance(e.code, int):
                raise
            notes += f"\n\nCould not subscribe {email}: {one_line(e)}"
    return notes


def move(gql, issue, to, frm):
    """Moves the issue to state `to` only from state `frm` (ids), re-read first: None once moved, else the state it is in
    (already `to`, or not `frm`: the caller logs a skip)."""
    now = gql(Q_ISSUE_STATE, i=issue)["issue"]["state"]["id"]
    if now != frm:
        return now
    call(gql, M_STATE, "issueUpdate", i=issue, s=to)
    return None


def comment_and_move(gql, issue, body, to, frm, emails=()):
    """subscribe, move, then body with the subscribe notes; returns move's result. Move first: a failed or skipped move
    posts no comment, so a retry never repeats it."""
    notes = subscribe(gql, issue, emails)
    left = move(gql, issue, to, frm)
    if left is None:
        comment(gql, issue, body + notes)
    return left


def last_move(nodes, states, actors=None):
    """Latest createdAt among the history nodes moved into states (by actors, when given), or None."""
    return max((parse_time(n["createdAt"]) for n in nodes
                if n["toStateId"] in states and (actors is None or n["actorId"] in actors)), default=None)


Q_USER = "query($e: String!) { users(filter: { email: { eqIgnoreCase: $e } }) { nodes { id } } }"


def user_id(gql, email):
    """Linear user id of an email (case-insensitive), or None."""
    nodes = gql(Q_USER, e=email)["users"]["nodes"]
    return nodes[0]["id"] if nodes else None


def humans(gql, cfg):
    """Linear user ids of the `human_members` emails, in order; one not found in Linear stops the caller."""
    emails = cfg.get("human_members") or []
    ids = [user_id(gql, e) for e in emails]
    if missing := [e for e, i in zip(emails, ids) if not i]:
        raise SystemExit(f"orchestrator/config.toml: human_members not found in Linear: {', '.join(missing)}")
    return ids


def role_ids(gql, runs):
    """{Linear user id: role} of the roles' accounts ({role: Role}); an account not found in Linear stops the caller."""
    out = {}
    for name, run in runs.items():
        uid = user_id(gql, run.account)
        if not uid:
            raise SystemExit(f"orchestrator/config.toml [roles.{name}]: account {run.account!r} not found in Linear")
        out[uid] = name
    return out


@dataclass(frozen=True)
class Team:
    """The orchestrator/config.toml team as checked by team(): {logical state: state id}."""
    id: str
    name: str
    states: dict


Q_TEAM = """query($t: ID) { teams(filter: { id: { eq: $t } }) { nodes { id name
  states(first: 100) { nodes { id } } } } }"""


def team(gql, cfg):
    """The orchestrator/config.toml team, checked in one query: it exists and holds every [states] id; a bad id stops the caller."""
    nodes = gql(Q_TEAM, t=cfg["team"])["teams"]["nodes"]
    if not nodes:
        raise SystemExit(f"orchestrator/config.toml: team {cfg['team']} not found in Linear")
    t = nodes[0]
    ids = {s["id"] for s in t["states"]["nodes"]}
    if bad := [f"{k} {cfg['states'][k]}" for k in config.STATES if cfg["states"][k] not in ids]:
        raise SystemExit(f"orchestrator/config.toml: [states] not workflow states of team {t['name']!r}: {', '.join(bad)}")
    return Team(t["id"], t["name"], {k: cfg["states"][k] for k in config.STATES})


Q_TASK_GROUP = "query($i: String!) { issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } } }"


def task_group(gql, cfg):
    """Checks, in one query, that task_label_group is a Linear label group and every [task_labels] id is one of its children; a failure stops the caller. Children are unpaginated (250): beyond that a valid id fails."""
    group = cfg["task_label_group"]
    try:
        label = gql(Q_TASK_GROUP, i=group)["issueLabel"]
    except SystemExit as e:
        raise SystemExit(f"orchestrator/config.toml: task_label_group {group} not found in Linear: {e.code}") from None
    if not label["isGroup"]:
        raise SystemExit(f"orchestrator/config.toml: task_label_group {group} is not a label group")
    ids = {c["id"] for c in label["children"]["nodes"]}
    if bad := [f"{task} {i}" for task, i in cfg["task_labels"].items() if i not in ids]:
        raise SystemExit(f"orchestrator/config.toml: [task_labels] not labels of task_label_group {group}: {', '.join(bad)}")
