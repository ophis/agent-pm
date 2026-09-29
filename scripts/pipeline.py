"""Shared by router.py, launch.py and promote.py: Linear access, paths and pipeline.toml.
Imports none of them. Needs Python 3.11+ (tomllib).
"""
import json
import os
import re
import string
import subprocess
import sys
import tomllib
import urllib.request
from dataclasses import dataclass
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "pipeline.toml")
WORK = os.path.join(ROOT, "work")
PROJECTS = os.path.expanduser("~/.claude/projects")
LOGS = os.path.join(ROOT, "logs")
RUNS_LOG = os.path.join(LOGS, "runs.log")
SESSION = "agent-pm"
# launchd starts jobs with /usr/bin:/bin:/usr/sbin:/sbin; tmux and claude live elsewhere.
PATH = f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin"


def linear_gql(query, **variables):
    key = subprocess.run(["security", "find-generic-password", "-a", "frank.agent.w", "-s", "linear-api-key", "-w"],
                         capture_output=True, text=True, check=True).stdout.strip()
    req = urllib.request.Request("https://api.linear.app/graphql",
                                 data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
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


SID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def transcript(issue, sid, projects=PROJECTS):
    """Session file for a run; sid must be a UUID (untrusted input) or this returns None."""
    if not SID_RE.fullmatch(sid):
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


TOP_KEYS = {"team", "human_members", "projects"}
PROJECT_KEYS = {"next", "prefix", "require_instructions", "role", "task"}
ROLE_KEYS = {"read_only", "memory"}
TASK_KEYS = {"model", "effort", "add_dirs", "repo_from_issue", "allowed_tools"}
SETTINGS = "role and task settings live in roles/<role>.toml and tasks/<task>.toml"
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REPO = "{repo}"
# Config the runs (--setting-sources user) and launchd trust; a writable memory dir must stay out of them.
PROTECTED = ("~/.claude", "~/Library/LaunchAgents")


@dataclass(frozen=True)
class Run:
    """A runnable project's task and role, resolved from roles/ and tasks/; every path is absolute."""
    task_name: str
    task: dict
    charter: str
    instructions: str
    memory: str | None
    read_only: tuple


def load_config(path=CONFIG):
    """Checks every consumer needs; runnable-project checks are in runnable()."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    projects = cfg.setdefault("projects", {})
    for name, p in projects.items():
        nxt = p.get("next")
        if nxt and "prefix" not in projects.get(nxt, {}):
            raise SystemExit(f"pipeline.toml: next of {name!r} must name a [projects] entry with a prefix")
        seen = {name}
        while nxt:
            if nxt in seen:
                raise SystemExit(f"pipeline.toml: the next chain from {name!r} has a cycle")
            seen.add(nxt)
            nxt = projects.get(nxt, {}).get("next")
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


def _role(name, r, root):
    """(read_only, memory) of roles/<name>.toml, normalized; a broken file stops the caller."""
    where = f"roles/{name}.toml"
    if extra := sorted(set(r) - ROLE_KEYS):
        raise SystemExit(f"{where} has unknown keys: {', '.join(extra)}")
    if not isinstance(r.get("read_only", []), list):
        raise SystemExit(f"{where}: read_only must be a list")
    read_only = []
    for entry in r.get("read_only", []):
        path = os.path.expanduser(entry)
        if entry != REPO and (not os.path.isabs(path) or ".." in path.split(os.sep) or "{" in path or "}" in path):
            raise SystemExit(f"{where}: read_only entries are absolute paths or {REPO}: {entry!r}")
        read_only.append(entry if entry == REPO else os.path.normpath(path))
    memory = r.get("memory")
    if memory is None:
        return tuple(read_only), None
    memory = os.path.normpath(os.path.expanduser(memory))
    if not os.path.isabs(memory) or not os.path.isdir(memory):
        raise SystemExit(f"{where}: memory must be an existing absolute directory: {r['memory']!r}")
    near = [root] + [p for p in read_only if p != REPO] + [os.path.expanduser(p) for p in PROTECTED]
    if any(overlaps(memory, b) for b in near):
        raise SystemExit(f"{where}: memory must not overlap the repo root, a read_only path or {', '.join(PROTECTED)}: {r['memory']!r}")
    return tuple(read_only), memory


def _task(name, t):
    where = f"tasks/{name}.toml"
    if extra := sorted(set(t) - TASK_KEYS):
        raise SystemExit(f"{where} has unknown keys: {', '.join(extra)}")
    if missing := [k for k in ("model", "effort") if not t.get(k)]:
        raise SystemExit(f"{where} has no {', '.join(missing)}")
    check_allowed_tools(where, t)


def registry(root=ROOT):
    """(roles, tasks) from root/roles and root/tasks, every pair validated: {name: (read_only, memory)}, {name: task table}."""
    roles = {name: _role(name, r, root) for name, r in _pairs(root, "roles").items()}
    tasks = _pairs(root, "tasks")
    for name, t in tasks.items():
        _task(name, t)
    return roles, tasks


def runnable(cfg, root=ROOT):
    """{project id: Run} of projects with role + task; a broken pipeline.toml, role or task stops the caller (fail loud)."""
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise SystemExit(f"pipeline.toml has unknown keys: {', '.join(extra)}; {SETTINGS}")
    roles, tasks = registry(root)
    out = {}
    for name, p in cfg["projects"].items():
        if extra := sorted(set(p) - PROJECT_KEYS):
            raise SystemExit(f"pipeline.toml: {name!r} has unknown keys: {', '.join(extra)}; {SETTINGS}")
        if "role" not in p and "task" not in p:
            continue
        if "role" not in p or "task" not in p:
            raise SystemExit(f"pipeline.toml: {name!r} needs both role and task")
        role, task = p["role"], p["task"]
        if role not in roles:
            raise SystemExit(f"pipeline.toml: {name!r} role {role!r} has no roles/<role>.md + .toml pair")
        if task not in tasks:
            raise SystemExit(f"pipeline.toml: {name!r} task {task!r} has no tasks/<task>.md + .toml pair")
        read_only, memory = roles[role]
        t = tasks[task]
        if REPO in read_only and (not t.get("repo_from_issue") or "allowed_tools" in t):
            raise SystemExit(f"pipeline.toml: {name!r}: role {role!r} has read_only {REPO}, so task {task!r} needs repo_from_issue and no allowed_tools")
        out[name] = Run(task, t, os.path.join(root, "roles", f"{role}.md"), os.path.join(root, "tasks", f"{task}.md"), memory, read_only)
    return out


def uncommitted(porcelain):
    """Registry paths in `git status --porcelain -z --ignored -uall` output: every tracked change,
    and untracked or ignored files only when they are .md or .toml (what registry() scans)."""
    out, entries = [], iter(porcelain.split("\0"))
    for entry in entries:
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            next(entries, None)  # -z puts a rename's or copy's source path in the next entry
        if code in ("??", "!!") and not path.endswith((".md", ".toml")):
            continue
        out.append(path)
    return out


def reviewer(gql, cfg):
    """Linear user id of the first `human_members` email, who is assigned issues that need human review; None if unset."""
    emails = cfg.get("human_members") or []
    if not emails:
        return None
    nodes = gql("query($e: String!) { users(filter: { email: { eqIgnoreCase: $e } }) { nodes { id } } }", e=emails[0])["users"]["nodes"]
    if not nodes:
        raise SystemExit(f"pipeline.toml: human_members {emails[0]!r} not found in Linear")
    return nodes[0]["id"]


def stage_order(cfg):
    """{name: position in its next chain}; projects outside a chain are 0."""
    projects = cfg["projects"]
    targets = {p["next"] for p in projects.values() if p.get("next")}
    order = {name: 0 for name in projects}
    for start in projects:
        if start in targets:
            continue
        name, i = start, 0
        while name:
            order[name] = i
            name, i = projects.get(name, {}).get("next"), i + 1
    return order
