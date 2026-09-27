"""Shared by router.py, launch.py and promote.py: Linear access, paths and pipeline.toml.
Imports none of them. Needs Python 3.11+ (tomllib).
"""
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "pipeline.toml")
WORK = os.path.join(ROOT, "work")
# claude keys transcripts by cwd, with "/" and "." replaced by "-"; must match the launcher's cwd (WORK).
TRANSCRIPTS = os.path.expanduser("~/.claude/projects/" + WORK.replace("/", "-").replace(".", "-"))
LOGS = os.path.join(ROOT, "logs")
RUNS_LOG = os.path.join(LOGS, "runs.log")
SESSION = "agent-pm"


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
