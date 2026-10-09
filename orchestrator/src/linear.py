"""Linear access: Keychain-keyed GraphQL transport, lookups of the config's users, team and task labels, the shared writes
and history, and small shared helpers, among them log, the one writer of orchestrator.jsonl.

AGENT_PM_LINEAR (tests only), when set and non-empty, names a JSON file {"url": str, "keys": {service: key}} standing in
for both api.linear.app and the Keychain. The URL guard admits only http://127.0.0.1:<port>/... or
http://[::1]:<port>/..., no user or password, no whitespace: a test key never leaves the machine. Like any env, it
reaches the agent run: the path, never a key.
"""
import functools
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config  # noqa: E402
import drive  # noqa: E402

SEAM = "AGENT_PM_LINEAR"
URL = "https://api.linear.app/graphql"
SECURITY = "/usr/bin/security"  # absolute: no PATH entry stands in for the Keychain


@functools.cache
def harness_service():
    """Keychain service of the harness account's Linear key: the orchestrator config's harness_key."""
    return config.load_config()["harness_key"]


def linear_gql(query, *, timeout=30, service=None, **variables):
    """Linear, at the seam's url or else URL, as the account whose key is service's (key), default the harness
    account's."""
    auth = key(service or harness_service(), timeout)
    seam = _seam()
    req = urllib.request.Request(seam[0] if seam else URL,
                                 data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": auth})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"linear api error: {body['errors']}")
    return body["data"]


def key(service, timeout):
    """service's Linear key: the seam's, else its Keychain item's secret. SystemExit, naming only service, when the seam
    lacks it or it is empty, not printable or holds whitespace (urllib's header error would echo it)."""
    seam = _seam()
    if seam is None:
        found = subprocess.run([SECURITY, "find-generic-password", "-s", service, "-w"],
                               capture_output=True, text=True, check=True, timeout=timeout).stdout.strip()
    elif service in seam[1]:
        found = seam[1][service]
    else:
        raise SystemExit(f"{SEAM}: no key for {service}")
    if not _clean(found):
        raise SystemExit(f"Linear key of {service}: empty, not printable or holds whitespace")
    return found


def has_key(service):
    """True when service has a key: in the seam's keys, else a Keychain item (its secret never read: no -w)."""
    seam = _seam()
    if seam is not None:
        return service in seam[1]
    return subprocess.run([SECURITY, "find-generic-password", "-s", service],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _seam():
    """None when $AGENT_PM_LINEAR is unset or empty, else (url, keys) from the file it names. SystemExit, naming the
    variable and the path, never the file's text, when it is unreadable, of another shape or its url fails the guard."""
    path = os.environ.get(SEAM)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            seam = json.load(f)
    except (OSError, ValueError):
        raise SystemExit(f"{SEAM} {path}: unreadable or not JSON") from None
    url, keys = (seam.get("url"), seam.get("keys")) if isinstance(seam, dict) else (None, None)
    if not isinstance(url, str) or not isinstance(keys, dict) or not all(isinstance(k, str) for k in keys.values()):
        raise SystemExit(f"{SEAM} {path}: not an object with a url string and a keys object of strings")
    if not _local(url):
        raise SystemExit(f"{SEAM} {path}: url not http://127.0.0.1:<port>/... or http://[::1]:<port>/...")
    return url, keys


def _local(url):
    """url passes the URL guard (the module docstring), parsed, never a prefix test."""
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return False
    return (_clean(url) and u.scheme == "http" and u.hostname in ("127.0.0.1", "::1") and port is not None
            and u.username is None and u.password is None)


def _clean(s):
    """s is non-empty, printable and holds no whitespace."""
    return bool(s) and s.isprintable() and not any(c.isspace() for c in s)


ISSUE_ID = r"[A-Z][A-Z0-9]*-\d+"
ONCE = ("src", "kind", "issue", "entry", "reason")
WINDOW = timedelta(hours=24)
CONFIG_ERRORS = (SystemExit, ValueError, OSError)   # load_config, runnable: a bad value, TOML, a missing file
_recent = {}


def log(src, kind, issue=None, *, once=False, dry=False, **fields):
    """Appends {"ts", "src", "kind", "issue", **fields} (None values left out) to <LOGS_DIR>/orchestrator.jsonl, unless
    dry; copies it to stderr when dry or a terminal (launchd's stderr is the file). once, unless dry: skipped when a
    line with the same ONCE values was written within WINDOW. Never raises OSError."""
    line = {k: v for k, v in {"ts": drive.stamp(), "src": src, "kind": kind, "issue": issue, **fields}.items()
            if v is not None}
    path, key = os.path.join(config.LOGS_DIR, "orchestrator.jsonl"), _key(line)
    if once and not dry and key in _recent_keys(path):
        return
    text = json.dumps(line, ensure_ascii=False)
    try:
        if not dry:
            os.makedirs(config.LOGS_DIR, exist_ok=True)
            drive.append_line(path, text + "\n")
            if path in _recent:
                _recent[path].add(key)
    except OSError:
        pass
    try:
        if dry or sys.stderr.isatty():
            print(drive.printable(text), file=sys.stderr, flush=True)
    except OSError:  # a gone tmux pane's EIO
        pass


def config_error(src, e, dry=False):
    """A config that does not load (load_config's or runnable's SystemExit, a TOML or file error; CONFIG_ERRORS):
    logged once a day, printed too on a terminal unless dry (the dry copy shows it). Returns the exit code, 1."""
    reason = one_line(e.code if isinstance(e, SystemExit) else e)
    log(src, "config-error", once=True, dry=dry, reason=reason)
    try:
        if sys.stderr.isatty() and not dry:  # elsewhere (launchd) stderr is the log
            print(f"{src}.py: {reason}", file=sys.stderr, flush=True)
    except OSError:
        pass
    return 1


def _key(line):
    return json.dumps([line.get(k) for k in ONCE])


def _recent_keys(path):
    """The ONCE keys of path's lines from the last WINDOW, read once per process."""
    if path not in _recent:
        cutoff, keys = datetime.now(timezone.utc) - WINDOW, set()
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
        _recent[path] = keys
    return _recent[path]


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


def reraise_signal(e):
    """The runner's signal handler raises SystemExit(128 + signum), which must reach drive's loop to kill claude;
    linear_gql's SystemExit (a str code) is an API error."""
    if isinstance(e, SystemExit) and isinstance(e.code, int):
        raise e


def subscribe(gql, issue, emails):
    """Subscribes each email; returns the failures as notes for a comment."""
    notes = ""
    for email in emails:
        try:
            call(gql, M_SUBSCRIBE, "issueSubscribe", i=issue, e=email)
        except (Exception, SystemExit) as e:
            reraise_signal(e)
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
    """The config's team as checked by team(): {logical state: state id}."""
    id: str
    name: str
    states: dict


Q_TEAM = """query($t: ID) { teams(filter: { id: { eq: $t } }) { nodes { id name
  states(first: 100) { nodes { id } } } } }"""


def team(gql, cfg):
    """The config's team, checked in one query: it exists and holds every [states] id; a bad id stops the caller."""
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
