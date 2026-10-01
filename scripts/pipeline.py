"""Shared by router.py, launch.py and promote.py: Linear access, paths and pipeline.toml.
Imports none of them. Needs Python 3.11+ (tomllib).
"""
import functools
import json
import os
import re
import string
import subprocess
import sys
import tomllib
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "pipeline.toml")
WORK = os.path.join(ROOT, "work")
PROJECTS = os.path.expanduser("~/.claude/projects")
LOGS = os.path.join(ROOT, "logs")
RUNS_LOG = os.path.join(LOGS, "runs.log")
# launchd starts jobs with /usr/bin:/bin:/usr/sbin:/sbin; tmux and claude live elsewhere.
PATH = f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin"


def session(role):
    """The tmux session of a role's runs."""
    return f"agent-pm-{role}"


@functools.cache
def harness_service():
    """Keychain service of the harness account's Linear key: pipeline.toml's harness_key."""
    return load_config()["harness_key"]


def linear_gql(query, *, timeout=30, **variables):
    key = subprocess.run(["security", "find-generic-password", "-s", harness_service(), "-w"],
                         capture_output=True, text=True, check=True, timeout=timeout).stdout.strip()
    req = urllib.request.Request("https://api.linear.app/graphql",
                                 data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"linear api error: {body['errors']}")
    return body["data"]


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


PLACEHOLDERS = {"worktree", "branch", "owner", "name", "default", "clone"}
# Build tools and runners execute repo-defined code whatever their arguments.
INTERPRETER_RE = re.compile(r"\b(?:(?:python[\d.]*|bash|sh|zsh|node|ruby|perl)\s+\S*[/.]"
                            r"|(?:make|npm|npx|pnpm|yarn|bun|cargo|go|pytest|uv)\b)")


def _root_forms(root):
    home = os.path.expanduser("~")
    if not root.startswith(home + os.sep):
        return [root]
    rest = root[len(home):]
    return [root, "~" + rest, "$HOME" + rest, "${HOME}" + rest]


def _fields(where, rule):
    try:
        return [field for _, field, _, _ in string.Formatter().parse(rule) if field is not None]
    except ValueError as e:
        raise SystemExit(f"{where}: allowed_tools rule is not a valid template ({e}): {rule!r}") from None


def check_allowed_tools(where, p, root=ROOT):
    """Trust model: no wildcards, no unknown placeholders, no ROOT, no interpreter-on-script, only with repo_from_issue."""
    tools = p.get("allowed_tools")
    if tools is None:
        return
    if not p.get("repo_from_issue"):
        raise SystemExit(f"{where}: allowed_tools without repo_from_issue")
    for rule in tools:
        if "*" in rule:
            raise SystemExit(f"{where}: allowed_tools rule has a wildcard: {rule!r}")
        if any(r in rule for r in _root_forms(root)):
            raise SystemExit(f"{where}: allowed_tools rule contains root: {rule!r}")
        if INTERPRETER_RE.search(rule):
            raise SystemExit(f"{where}: allowed_tools rule runs an interpreter on a script: {rule!r}")
        for field in _fields(where, rule):
            if field not in PLACEHOLDERS:
                raise SystemExit(f"{where}: allowed_tools rule has unknown placeholder {{{field}}}: {rule!r}")


TOP_KEYS = {"team", "states", "human_members", "harness_key", "task_label_group", "task_labels", "docs", "roles", "project_repos"}
# Logical workflow states the code uses -> the name the docs use (a label; Linear is always queried by id).
STATES = {"todo": "Todo", "in_progress": "In Progress", "in_review": "In Review",
          "handoff": "Handoff", "done": "Done", "canceled": "Canceled"}
DOCS_KEYS = ("repo", "clone", "branch")
PIPELINE_ROLE_KEYS = {"next", "require_instructions"}
ROLE_KEYS = {"read_only", "memory", "tasks", "account", "key"}
TASK_KEYS = {"model", "effort", "add_dirs", "repo_from_issue", "read_repo", "allowed_tools", "prefix"}
SETTINGS = "role and task settings live in roles/<role>.toml and tasks/<task>.toml"
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REPO = "{repo}"
DOCS_CLONE = "{docs_clone}"  # a static config value, resolved here; {repo} is per issue, resolved by the launcher
# A clone with one of these would break the launcher's Edit(//<clone>/**) deny rule or the prompt's Docs: line.
CLONE_BAD_RE = re.compile(r"[\x00-\x1f\x7f*?\[\](){},]")
# Config the runs (--setting-sources user) and launchd trust; a writable memory dir must stay out of them.
PROTECTED = ("~/.claude", "~/Library/LaunchAgents")


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


def _check_docs(cfg):
    docs = cfg.get("docs")
    if not isinstance(docs, dict):
        docs = {}
    if missing := [k for k in DOCS_KEYS if k not in docs]:
        raise SystemExit(f"pipeline.toml: [docs] is missing: {', '.join(missing)}")
    if extra := sorted(set(docs) - set(DOCS_KEYS)):
        raise SystemExit(f"pipeline.toml: [docs] has unknown keys: {', '.join(extra)}")
    if not repo_slug(docs["repo"]):
        raise SystemExit(f"pipeline.toml: docs.repo must be <owner>/<name>: {docs['repo']!r}")
    clone = docs["clone"]
    path = os.path.normpath(os.path.expanduser(clone)) if isinstance(clone, str) else ""
    if (not os.path.isabs(path) or CLONE_BAD_RE.search(path)
            or any(overlaps(path, b) for b in [ROOT] + [os.path.expanduser(p) for p in PROTECTED])):
        raise SystemExit(f"pipeline.toml: docs.clone must be an absolute path (~ allowed) outside the repo root and "
                         f"{', '.join(PROTECTED)}, without control characters or any of *?[](){{}},: {clone!r}")
    branch = docs["branch"]
    if not isinstance(branch, str) or not REF.fullmatch(branch):
        raise SystemExit(f"pipeline.toml: docs.branch must be a git ref name: {branch!r}")
    docs["clone"] = path


@dataclass(frozen=True)
class Role:
    """roles/<role>.toml, validated; key is a Keychain service name, never the secret."""
    read_only: tuple
    memory: str | None
    tasks: tuple
    account: str
    key: str


@dataclass(frozen=True)
class Run:
    """A role's task, resolved from roles/ and tasks/; every path is absolute. runnable() gives the default task (the first of tasks), with_task() another.
    key is the role's Keychain service name, account its Linear email, tasks the role's {name: task table} in its order."""
    task_name: str
    task: dict
    charter: str
    instructions: str
    memory: str | None
    read_only: tuple
    key: str
    account: str
    tasks: dict

    def with_task(self, name):
        """This run for task `name` of the same role; KeyError when the role does not have it."""
        return replace(self, task_name=name, task=self.tasks[name],
                       instructions=os.path.join(os.path.dirname(self.instructions), f"{name}.md"))


def load_config(path=CONFIG):
    """Checks every consumer needs; the role checks are in runnable(), which every consumer calls."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    _check_ids(cfg)
    _check_docs(cfg)
    hk = cfg.get("harness_key")
    if not isinstance(hk, str) or not hk:
        raise SystemExit(f"pipeline.toml: harness_key must be a Keychain service name: {hk!r}")
    roles = cfg.setdefault("roles", {})
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


def _under(path, base):
    return os.path.commonpath([path, base]) == base


def overlaps(a, b):
    """True when one path is at or under the other, compared by realpath."""
    a, b = os.path.realpath(a), os.path.realpath(b)
    return _under(a, b) or _under(b, a)


def _pairs(root, kind):
    """{name: parsed <kind>/<name>.toml} for every .md + .toml pair in root/<kind>; other files are ignored."""
    d = os.path.join(root, kind)
    found = {}
    for f in sorted(os.listdir(d)):
        stem, ext = os.path.splitext(f)
        if ext in (".md", ".toml") and os.path.isfile(os.path.join(d, f)):
            found.setdefault(stem, set()).add(ext)
    if kind == "roles" and ".toml" in found.pop("principles", set()):
        raise SystemExit("roles/principles.toml: principles.md is not a role and has no .toml")
    out = {}
    for name, exts in found.items():
        where = f"{kind}/{name}{min(exts)}"
        if not NAME_RE.fullmatch(name):
            raise SystemExit(f"{where}: names are lowercase-kebab")
        if len(exts) == 1:
            (ext,) = exts
            raise SystemExit(f"{kind}/{name}{ext}: has no {kind}/{name}{'.toml' if ext == '.md' else '.md'}")
        try:
            with open(os.path.join(d, f"{name}.toml"), "rb") as f:
                out[name] = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{kind}/{name}.toml: {e}") from None
    return out


def _role(name, r, root, resolve):
    """Role of roles/<name>.toml, normalized; a broken file stops the caller."""
    where = f"roles/{name}.toml"
    if extra := sorted(set(r) - ROLE_KEYS):
        raise SystemExit(f"{where} has unknown keys: {', '.join(extra)}")
    if not isinstance(r.get("read_only", []), list):
        raise SystemExit(f"{where}: read_only must be a list")
    read_only = []
    for entry in r.get("read_only", []):
        path = os.path.expanduser(resolve(entry))
        if entry != REPO and (not os.path.isabs(path) or ".." in path.split(os.sep) or "{" in path or "}" in path):
            raise SystemExit(f"{where}: read_only entries are absolute paths, {REPO} or {DOCS_CLONE}: {entry!r}")
        read_only.append(entry if entry == REPO else os.path.normpath(path))
    memory = r.get("memory")
    if memory is not None:
        memory = os.path.normpath(os.path.expanduser(memory))
        if not os.path.isabs(memory) or not os.path.isdir(memory):
            raise SystemExit(f"{where}: memory must be an existing absolute directory: {r['memory']!r}")
        near = [root] + [p for p in read_only if p != REPO] + [os.path.expanduser(p) for p in PROTECTED]
        if any(overlaps(memory, b) for b in near):
            raise SystemExit(f"{where}: memory must not overlap the repo root, a read_only path or {', '.join(PROTECTED)}: {r['memory']!r}")
    tasks, account, key = r.get("tasks"), r.get("account"), r.get("key")
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, str) and t for t in tasks):
        raise SystemExit(f"{where}: tasks must be a non-empty list of task names: {tasks!r}")
    if not isinstance(account, str) or not account:
        raise SystemExit(f"{where}: account must be the role's Linear email: {account!r}")
    if not isinstance(key, str) or not key:
        raise SystemExit(f"{where}: key must be a Keychain service name: {key!r}")
    return Role(tuple(read_only), memory, tuple(tasks), account, key)


def _task(name, t, resolve):
    """Task table of tasks/<name>.toml with add_dirs resolved; a broken file stops the caller."""
    where = f"tasks/{name}.toml"
    if extra := sorted(set(t) - TASK_KEYS):
        raise SystemExit(f"{where} has unknown keys: {', '.join(extra)}")
    if missing := [k for k in ("model", "effort") if not t.get(k)]:
        raise SystemExit(f"{where} has no {', '.join(missing)}")
    if t.get("read_repo") and t.get("repo_from_issue"):
        raise SystemExit(f"{where}: read_repo and repo_from_issue are exclusive")
    check_allowed_tools(where, t)
    if "add_dirs" not in t:
        return t
    dirs = t["add_dirs"]
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise SystemExit(f"{where}: add_dirs must be a list of paths: {dirs!r}")
    if bad := [d for d in dirs if d != DOCS_CLONE and ("{" in d or "}" in d)]:
        raise SystemExit(f"{where}: add_dirs entries have no braces except {DOCS_CLONE}: {bad[0]!r}")
    return {**t, "add_dirs": [resolve(d) for d in dirs]}


def registry(root=ROOT, docs_clone=None):
    """(roles, tasks) from root/roles and root/tasks, every pair validated: {name: Role}, {name: task table}.
    {docs_clone} entries become docs_clone, else the repo's pipeline.toml clone, which is read only when one occurs."""
    def resolve(entry):
        nonlocal docs_clone
        if entry != DOCS_CLONE:
            return entry
        if docs_clone is None:
            docs_clone = load_config()["docs"]["clone"]
        return docs_clone

    roles = {name: _role(name, r, root, resolve) for name, r in _pairs(root, "roles").items()}
    tasks = {name: _task(name, t, resolve) for name, t in _pairs(root, "tasks").items()}
    owner = {}
    for name, r in roles.items():
        if unknown := [t for t in r.tasks if t not in tasks]:
            raise SystemExit(f"roles/{name}.toml: tasks {', '.join(unknown)} have no tasks/<task>.md + .toml pair")
        if r.key in owner:
            raise SystemExit(f"roles/{name}.toml: key {r.key!r} is also roles/{owner[r.key]}.toml's")
        owner[r.key] = name
    return roles, tasks


def runnable(cfg, root=ROOT):
    """{role: Run} of every role, each its default task's (Run.with_task gives another of the role's tasks); a broken pipeline.toml, role or task stops the caller (fail loud)."""
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise SystemExit(f"pipeline.toml has unknown keys: {', '.join(extra)}; {SETTINGS}")
    roles, tasks = registry(root, cfg["docs"]["clone"])
    for name, r in roles.items():
        if r.key == cfg["harness_key"]:
            raise SystemExit(f"roles/{name}.toml: key {r.key!r} is pipeline.toml's harness_key")
    for name, p in cfg["roles"].items():
        if name not in roles:
            raise SystemExit(f"pipeline.toml: [roles.{name}] has no roles/<role>.md + .toml pair")
        if extra := sorted(set(p) - PIPELINE_ROLE_KEYS):
            raise SystemExit(f"pipeline.toml: [roles.{name}] has unknown keys: {', '.join(extra)}; {SETTINGS}")
        nxt = p.get("next")
        if not nxt:
            continue
        if nxt not in roles:
            raise SystemExit(f"pipeline.toml: next of {name!r} names undefined role {nxt!r}")
        nxt_task = roles[nxt].tasks[0]
        if not tasks[nxt_task].get("prefix"):
            raise SystemExit(f"pipeline.toml: next of {name!r} is role {nxt!r}, whose default task {nxt_task!r} has no prefix")
    for task in cfg["task_labels"]:
        if task not in tasks:
            raise SystemExit(f"pipeline.toml: task_labels.{task} has no tasks/{task}.md + .toml pair")
    out = {}
    for name, r in roles.items():
        role_tasks = {t: tasks[t] for t in r.tasks}
        if REPO in r.read_only:
            for i, (t, table) in enumerate(role_tasks.items()):
                if not table.get("repo_from_issue") or "allowed_tools" in table:
                    which = f"default task {t!r}" if i == 0 else f"task {t!r}"
                    raise SystemExit(f"roles/{name}.toml: read_only {REPO} needs {which} with repo_from_issue and no allowed_tools")
        task = r.tasks[0]
        out[name] = Run(task, tasks[task], os.path.join(root, "roles", f"{name}.md"), os.path.join(root, "tasks", f"{task}.md"),
                        r.memory, r.read_only, r.key, r.account, role_tasks)
    return out


def hands_off_to_repo(cfg, runs, role):
    """True when role's next role (pipeline.toml) runs a repo_from_issue task: the role's runs need the project's repo."""
    nxt = cfg["roles"].get(role, {}).get("next")
    return bool(nxt and runs[nxt].task.get("repo_from_issue"))


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
    """The role in runs ({role: Run}) whose account is email (case-insensitive), or None."""
    return next((r for r, run in runs.items() if email and run.account.lower() == email.lower()), None)


def role_ids(gql, runs):
    """{Linear user id: role} of the runs' accounts; an account not found in Linear stops the caller."""
    out = {}
    for name, run in runs.items():
        uid = user_id(gql, run.account)
        if not uid:
            raise SystemExit(f"roles/{name}.toml: account {run.account!r} not found in Linear")
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
