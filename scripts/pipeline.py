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


def _fields(name, rule):
    try:
        return [field for _, field, _, _ in string.Formatter().parse(rule) if field is not None]
    except ValueError as e:
        raise SystemExit(f"pipeline.toml: {name!r} allowed_tools rule is not a valid template ({e}): {rule!r}") from None


def check_allowed_tools(name, p, root=ROOT):
    """Trust model: no wildcards, no unknown placeholders, no ROOT, no interpreter-on-script, only with repo_from_issue."""
    tools = p.get("allowed_tools")
    if tools is None:
        return
    if not p.get("repo_from_issue"):
        raise SystemExit(f"pipeline.toml: {name!r} has allowed_tools without repo_from_issue")
    for rule in tools:
        if "*" in rule:
            raise SystemExit(f"pipeline.toml: {name!r} allowed_tools rule has a wildcard: {rule!r}")
        if any(r in rule for r in _root_forms(root)):
            raise SystemExit(f"pipeline.toml: {name!r} allowed_tools rule contains root: {rule!r}")
        if INTERPRETER_RE.search(rule):
            raise SystemExit(f"pipeline.toml: {name!r} allowed_tools rule runs an interpreter on a script: {rule!r}")
        for field in _fields(name, rule):
            if field not in PLACEHOLDERS:
                raise SystemExit(f"pipeline.toml: {name!r} allowed_tools rule has unknown placeholder {{{field}}}: {rule!r}")


def load_config(path=CONFIG):
    """Checks every consumer needs; runnable-project checks are in runnable()."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    projects = cfg.setdefault("projects", {})
    for name, p in projects.items():
        check_allowed_tools(name, p)
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


def runnable(cfg, root=ROOT):
    """{name: entry} of projects with `instructions`; a broken entry stops the caller (fail loud)."""
    out = {}
    for name, p in cfg["projects"].items():
        if "instructions" not in p:
            continue
        missing = [k for k in ("model", "effort") if not p.get(k)]
        if missing:
            raise SystemExit(f"pipeline.toml: {name!r} has instructions but no {', '.join(missing)}")
        if not os.path.isfile(os.path.join(root, p["instructions"])):
            raise SystemExit(f"pipeline.toml: {name!r} instructions not found: {p['instructions']}")
        out[name] = p
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
