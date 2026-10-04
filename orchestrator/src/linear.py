"""Linear access: Keychain-keyed GraphQL transport, lookups of the config's users, team and task labels, and small shared helpers.
Imports config, which puts core/src on sys.path.
"""
import functools
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime

import config

SHORT = 60


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


def err_text(res):
    return (res.stderr or "").strip()[:200]


def atomic_write(path, text):
    """Write path through a fresh temp file and os.replace: a planted symlink at path is replaced, never followed."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr, flush=True)


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


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
