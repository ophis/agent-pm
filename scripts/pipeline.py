"""Shared by the harness scripts: Linear access, paths, pipeline.toml, core config with its [core] overlay, and TASKS.
Imports none of them. Needs Python 3.11+ (tomllib).
"""
import functools
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "pipeline.toml")
CORE = os.path.join(ROOT, "core")
WORK = os.path.join(ROOT, "work")
PROJECTS = os.path.expanduser("~/.claude/projects")
LOGS = os.path.join(ROOT, "logs")
RUNS_LOG = os.path.join(LOGS, "runs.log")
# launchd starts jobs with /usr/bin:/bin:/usr/sbin:/sbin; tmux and claude live elsewhere.
PATH = f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin"
SHORT, LONG = 60, 600

# No scripts module may be named clients, compose, drive or repo: these come from core/src.
sys.path.insert(0, os.path.join(CORE, "src"))
import clients  # noqa: E402
import compose  # noqa: E402


def session(role):
    """The tmux session of a role's runs."""
    return f"agent-pm-{role}"


@functools.cache
def harness_service():
    """Keychain service of the harness account's Linear key: pipeline.toml's harness_key."""
    return load_config()["harness_key"]


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


def sh_run(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          env={**os.environ, "PATH": PATH})


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


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def project_log(name, logs=LOGS):
    d = os.path.join(logs, "projects")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{slug(name)}.log")


def escape(path):
    """Claude Code's folder name for a cwd: every non-alphanumeric character becomes "-"."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def run_dir(issue):
    return os.path.join(WORK, issue)


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
NAME = r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}"
REF = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+")


def repo_slug(value):
    """(owner, name) of a bare `owner/name`, else None."""
    m = re.fullmatch(rf"({OWNER})/({NAME})", value) if isinstance(value, str) else None
    return m.groups() if m and m.group(2) not in (".", "..") else None


def transcript(issue, sid, projects=PROJECTS):
    """Session file for a run; sid must be a UUID (untrusted input) or this returns None."""
    if not UUID_RE.fullmatch(sid):
        return None
    return os.path.join(projects, escape(run_dir(issue)), f"{sid}.jsonl")


TOP_KEYS = {"team", "states", "human_members", "harness_key", "task_label_group", "task_labels", "roles", "project_repos", "core"}
# Logical workflow states the code uses -> the name the docs use (a label; Linear is always queried by id).
STATES = {"todo": "Todo", "in_progress": "In Progress", "in_review": "In Review",
          "handoff": "Handoff", "done": "Done", "canceled": "Canceled"}
ROLE_KEYS = {"account", "key", "next", "require_instructions"}


@dataclass(frozen=True)
class Task:
    """Per-task data. Readers: kind → inputs/run; prefix → promote, writeback retitle; the rest → writeback."""
    kind: Literal["research", "design", "build"]
    prefix: str = ""                    # issue title prefix: promote child titles, retitle
    start: str = ""                     # start comment lead
    done: str = ""                      # done comment lead
    question: str = ""                  # needs_input comment lead
    failed: str = ""                    # failed comment lead
    failed_new: str = "in_review"       # state after failed on a new run (resume: in_review)
    retitle: bool = False               # done → title "<prefix>: <outcome title>"
    approve: bool = False               # done → approve line
    files: bool = False                 # done/failed → outcome files as Spec/Plan comments
    hint: str = ""                      # appended to the needs_input footer


REPO_HINT = "To change the target repo, edit the description's `Repo:` line."
TASKS = {
    "deep-research":  Task("research", start="Research started:", failed_new="todo", hint=REPO_HINT),
    "light-research": Task("research", start="Research started:", failed_new="todo", hint=REPO_HINT),
    "product-design": Task("design", prefix="PRD", start="PRD started:", retitle=True, approve=True),
    "engineering":    Task("build", prefix="ENG", start="Build started:", done="Build ready:",
                           question="Question:", failed="Build failed:", files=True),
}


@dataclass(frozen=True)
class Role:
    """A pipeline.toml role checked by runnable(); key is a Keychain service name, never the secret."""
    account: str        # Linear email
    key: str            # Keychain service of its API key
    tasks: tuple        # core config tasks, default_task first, then core order

    @property
    def default(self):
        return self.tasks[0]


@dataclass(frozen=True)
class Docs:
    """Where core's document tasks publish: one github.com repo and branch, a dir per task."""
    repo: str           # owner/name
    branch: str
    dirs: dict          # task → output dir


# Core's checkout dirs under a run's workdir: config commands' `--dir {{workdir}}/src`, the github destination's
# `<Workdir>/publish`.
CLONES = ("src", "publish")


def _uuid(v):
    return isinstance(v, str) and bool(UUID_RE.fullmatch(v))


def _check_ids(cfg):
    if not _uuid(cfg.get("team")):
        raise SystemExit(f"pipeline.toml: team must be a Linear team id (UUID): {cfg.get('team')!r}")
    states = cfg.get("states")
    if not isinstance(states, dict):
        states = {}
    if missing := [k for k in STATES if k not in states]:
        raise SystemExit(f"pipeline.toml: [states] is missing: {', '.join(missing)}")
    if extra := sorted(set(states) - set(STATES)):
        raise SystemExit(f"pipeline.toml: [states] has unknown keys: {', '.join(extra)}")
    for k in STATES:
        if not _uuid(states[k]):
            raise SystemExit(f"pipeline.toml: states.{k} must be a Linear workflow state id (UUID): {states[k]!r}")
    if not _uuid(cfg.get("task_label_group")):
        raise SystemExit(f"pipeline.toml: task_label_group must be a Linear label group id (UUID): {cfg.get('task_label_group')!r}")


def load_config(path=CONFIG):
    """Checks every consumer needs; the checks against core config are in runnable(), which every consumer calls."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    _check_ids(cfg)
    hk = cfg.get("harness_key")
    if not isinstance(hk, str) or not hk:
        raise SystemExit(f"pipeline.toml: harness_key must be a Keychain service name: {hk!r}")
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise SystemExit(f"pipeline.toml: unknown keys: {', '.join(extra)}")
    roles = cfg.setdefault("roles", {})
    for name, p in roles.items():
        if extra := sorted(set(p) - ROLE_KEYS):
            raise SystemExit(f"pipeline.toml: [roles.{name}] has unknown keys: {', '.join(extra)}")
        for k, what in (("account", "the role's Linear email"), ("key", "a Keychain service name")):
            if not isinstance(p.get(k), str) or not p[k]:
                raise SystemExit(f"pipeline.toml: [roles.{name}] {k} must be {what}: {p.get(k)!r}")
    for name in roles:
        nxt, seen = roles[name].get("next"), {name}
        while nxt:
            if nxt in seen:
                raise SystemExit(f"pipeline.toml: the next chain from {name!r} has a cycle")
            seen.add(nxt)
            nxt = roles.get(nxt, {}).get("next")
    repos = cfg.setdefault("project_repos", {})
    if not isinstance(repos, dict):
        raise SystemExit('pipeline.toml: project_repos must be a table of "<Linear project id>" = "<owner>/<name>"')
    for k, v in repos.items():
        if not _uuid(k) or not repo_slug(v):
            raise SystemExit(f"pipeline.toml: project_repos.{k} must map a Linear project id (UUID) to <owner>/<name>: {v!r}")
    labels = cfg.setdefault("task_labels", {})
    if not isinstance(labels, dict):
        raise SystemExit('pipeline.toml: task_labels must be a table of <task> = "<Linear label id>"')
    seen = {}
    for k, v in labels.items():
        if not _uuid(v):
            raise SystemExit(f"pipeline.toml: task_labels.{k} must be a Linear label id (UUID): {v!r}")
        if v in seen:
            raise SystemExit(f"pipeline.toml: task_labels.{seen[v]} and task_labels.{k} have the same label id {v}")
        seen[v] = k
    return cfg


def _fill_root(value, root):
    if isinstance(value, str):
        return value.replace("{{root}}", shlex.quote(root))
    if isinstance(value, dict):
        return {k: _fill_root(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_root(v, root) for v in value]
    return value


def _check_overlay_keys(table, allowed, where):
    if extra := sorted(set(table) - allowed):
        raise SystemExit(f"pipeline.toml [core]: unknown key {extra[0]!r} in {where}")


def overlay(root=ROOT):
    """<root>/pipeline.toml's [core] table ({} without one) with {{root}} filled; keys are checked here, since core checks
    only its own config's."""
    layer = _fill_root(load_config(os.path.join(root, "pipeline.toml")).get("core", {}), root)
    _check_overlay_keys(layer, compose.RUN_KEYS | {"roles"}, "the global table")
    for r, role in layer.get("roles", {}).items():
        _check_overlay_keys(role, compose.RUN_KEYS | {"tasks"}, f"roles.{r}")
        for t, task in role.get("tasks", {}).items():
            _check_overlay_keys(task, compose.RUN_KEYS, f"roles.{r}.tasks.{t}")
    return layer


def layers(root=ROOT):
    """The orchestrator's config layers, applied after core's client config; the one source for run_config and run.py."""
    return [overlay(root)]


def run_config(role, task, root=ROOT):
    """compose.RunConfig of role/task: core config, then the claude client config, then layers(root)."""
    core = os.path.join(root, "core")
    return compose.load_run(core, role, task, layers=[clients.load_config("claude", core), *layers(root)])


def runnable(cfg, root=ROOT):
    """{role: Role} of pipeline.toml's roles in its order, checked against core config with overlay(root) and TASKS; a
    broken one stops the caller (fail loud). Core roles absent from pipeline.toml are not orchestrated."""
    with open(os.path.join(root, "core", compose.CONFIG), "rb") as f:
        core_roles = tomllib.load(f).get("roles", {})
    out = {}
    for name, p in cfg["roles"].items():
        if name not in core_roles:
            raise SystemExit(f"pipeline.toml: role {name!r} is not in core/config/config.toml")
        default, tasks = core_roles[name].get("default_task"), list(core_roles[name].get("tasks", {}))
        tasks = tuple(dict.fromkeys([default, *tasks] if default else tasks))
        for t in tasks:
            if t not in TASKS:
                raise SystemExit(f"pipeline.toml: {name}'s task {t!r} has no entry in pipeline.TASKS")
            try:
                run_config(name, t, root)
            except compose.ConfigError as e:
                raise SystemExit(f"core: {e}") from None
        out[name] = Role(p["account"], p["key"], tasks)
    owner = {}
    for name, r in out.items():
        if r.key == cfg["harness_key"]:
            raise SystemExit(f"pipeline.toml: [roles.{name}] key {r.key!r} is harness_key")
        if r.key in owner:
            raise SystemExit(f"pipeline.toml: [roles.{name}] key {r.key!r} is also [roles.{owner[r.key]}]'s")
        owner[r.key] = name
    for name, p in cfg["roles"].items():
        nxt = p.get("next")
        if not nxt:
            continue
        if nxt not in out:
            raise SystemExit(f"pipeline.toml: next of {name!r} names undefined role {nxt!r}")
        if not TASKS[out[nxt].default].prefix:
            raise SystemExit(f"pipeline.toml: next of {name!r} is role {nxt!r}, whose default task {out[nxt].default!r} has no prefix")
    tasks = {t for r in out.values() for t in r.tasks}
    for task in cfg["task_labels"]:
        if task not in tasks:
            raise SystemExit(f"pipeline.toml: task_labels.{task} is not a task of a role in pipeline.toml")
    docs(out, root)
    return out


def docs(roles, root=ROOT):
    """Docs of the github outputs among roles' tasks ({role: Role}); they must share one github.com repo and branch."""
    outs = {t: o for r, role in roles.items() for t in role.tasks if (o := run_config(r, t, root).output)["type"] == "github"}
    targets = {(o.get("repo"), o.get("branch"), o.get("host", "github.com")) for o in outs.values()}
    repo, branch, host = targets.pop() if len(targets) == 1 else (None, None, None)
    if not repo or not branch or host != "github.com":
        raise SystemExit("core: document tasks must publish to one github.com repo and branch")
    return Docs(repo, branch, {t: o["dir"] for t, o in outs.items()})


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
        raise SystemExit(f"pipeline.toml: human_members not found in Linear: {', '.join(missing)}")
    return ids


def role_for(runs, email):
    """The role in runs ({role: Role}) whose account is email (case-insensitive), or None."""
    return next((r for r, run in runs.items() if email and run.account.lower() == email.lower()), None)


def role_ids(gql, runs):
    """{Linear user id: role} of the roles' accounts ({role: Role}); an account not found in Linear stops the caller."""
    out = {}
    for name, run in runs.items():
        uid = user_id(gql, run.account)
        if not uid:
            raise SystemExit(f"pipeline.toml [roles.{name}]: account {run.account!r} not found in Linear")
        out[uid] = name
    return out


@dataclass(frozen=True)
class Team:
    """The pipeline.toml team as checked by team(): {logical state: state id}."""
    id: str
    name: str
    states: dict


Q_TEAM = """query($t: ID) { teams(filter: { id: { eq: $t } }) { nodes { id name
  states(first: 100) { nodes { id } } } } }"""


def team(gql, cfg):
    """The pipeline.toml team, checked in one query: it exists and holds every [states] id; a bad id stops the caller."""
    nodes = gql(Q_TEAM, t=cfg["team"])["teams"]["nodes"]
    if not nodes:
        raise SystemExit(f"pipeline.toml: team {cfg['team']} not found in Linear")
    t = nodes[0]
    ids = {s["id"] for s in t["states"]["nodes"]}
    if bad := [f"{k} {cfg['states'][k]}" for k in STATES if cfg["states"][k] not in ids]:
        raise SystemExit(f"pipeline.toml: [states] not workflow states of team {t['name']!r}: {', '.join(bad)}")
    return Team(t["id"], t["name"], {k: cfg["states"][k] for k in STATES})


Q_TASK_GROUP = "query($i: String!) { issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } } }"


def task_group(gql, cfg):
    """Checks, in one query, that task_label_group is a Linear label group and every [task_labels] id is one of its children; a failure stops the caller. Children are unpaginated (250): beyond that a valid id fails."""
    group = cfg["task_label_group"]
    try:
        label = gql(Q_TASK_GROUP, i=group)["issueLabel"]
    except SystemExit as e:
        raise SystemExit(f"pipeline.toml: task_label_group {group} not found in Linear: {e.code}") from None
    if not label["isGroup"]:
        raise SystemExit(f"pipeline.toml: task_label_group {group} is not a label group")
    ids = {c["id"] for c in label["children"]["nodes"]}
    if bad := [f"{task} {i}" for task, i in cfg["task_labels"].items() if i not in ids]:
        raise SystemExit(f"pipeline.toml: [task_labels] not labels of task_label_group {group}: {', '.join(bad)}")


def stage_order(cfg):
    """{role: position in its next chain}; roles outside a chain are 0."""
    roles = cfg["roles"]
    targets = {p["next"] for p in roles.values() if p.get("next")}
    order = {name: 0 for name in roles}
    for start in roles:
        if start in targets:
            continue
        name, i = start, 0
        while name:
            order[name] = i
            name, i = roles.get(name, {}).get("next"), i + 1
    return order
