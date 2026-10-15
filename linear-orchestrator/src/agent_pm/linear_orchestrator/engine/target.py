"""An issue's target repo: its description's Repo: line or its project's mapping, checked on GitHub
(check), and the local clone its checkout may use (with_clone).
"""

import json
import os
import re
from dataclasses import replace

from agent_pm.delegate import Layout
from agent_pm.linear_orchestrator.engine import config, issues
from agent_pm.linear_orchestrator.engine.config import CLONES, REF, TASKS
from agent_pm.linear_orchestrator.engine.types import Invalid, Target, Transient
from agent_pm.utils import GUARD, SHORT, Repo, common_dir, err_text, repo_slug, under
from agent_pm.utils import Invalid as RepoInvalid
from agent_pm.utils import checkout as clone_checkout

MAPPED = "project mapping "


def _one(value):
    v = value.strip()
    m = re.fullmatch(r"\[[^\]]*\]\(<?([^)>]+)>?\)", v)
    if m:
        v = m.group(1).strip()
    if v.startswith("git@github.com:") and not v.endswith("/"):
        v = "https://github.com/" + v.removeprefix("git@github.com:")
    if not v.startswith("https://github.com/"):
        return repo_slug(v)
    try:
        repo = Repo.parse(v)
    except RepoInvalid:
        return None
    return repo.owner, repo.name


def parse_repo(description) -> tuple[str, str] | Invalid:
    found = []
    for line in issues.head(description).splitlines():
        m = re.match(
            r"^\s*(?:[-*>]\s+)?(?:\*\*|`)?repo:(?:\*\*)?\s*(.+?)\s*$", line.rstrip("\r"), re.I
        )
        if m:
            one = _one(m.group(1).strip("`*").strip())
            if not one:
                return Invalid(f"unreadable Repo line: {m.group(1)[:80]!r}")
            found.append(one)
    distinct = {(o.lower(), n.lower()) for o, n in found}
    if not found:
        return Invalid(f"no Repo: line in the description or its {issues.SECTIONS[1]}")
    if len(distinct) > 1:
        return Invalid("several different Repo: values")
    return found[0]


NO_LINE = parse_repo("")


def slug(title):
    t = re.sub(r"^[A-Z][A-Z0-9]*: ", "", title, count=1).lower()
    s = "-".join(re.findall(r"[a-z0-9]+", t))[:40].rstrip("-")
    return s or "build"


def pr_title(ident, title):
    # safe in single quotes
    t = f"{ident}: " + re.sub(r"^[A-Z][A-Z0-9]*: ", "", title, count=1)
    return " ".join(re.sub(r"[^\w .,:()/-]", " ", t).split())


def research_repo(issue, repos) -> Target | None:
    if parse_repo(issue.description) != NO_LINE or issue.project_id not in repos:
        return None
    one = repo_slug(repos[issue.project_id])
    return Target(*one) if one else None


def _repo_info(o, n, run):
    res = run(["gh", "api", f"repos/{o}/{n}"], SHORT)
    if res.returncode != 0:
        code = re.search(r"HTTP (\d{3})", res.stderr or "")
        if code and code.group(1) in ("403", "404"):
            return Invalid(f"{o}/{n}: not found or no access (HTTP {code.group(1)})")
        return Transient(f"gh api repos/{o}/{n}: {err_text(res)}")
    try:
        data = json.loads(res.stdout)
        full, push, default = (
            repo_slug(data["full_name"]),
            data["permissions"]["push"],
            data["default_branch"],
        )
    except (ValueError, KeyError, TypeError):
        full = None
    if not full:
        return Transient(f"gh api repos/{o}/{n}: unusable response")
    return full, push is True, default


def checkout(ident, owner, name, *, work) -> str:
    return clone_checkout(
        os.path.join(Layout(os.path.join(work, ident)).agent, CLONES[0]), owner, name, ident
    )


def _branches(ident, o, n, *, run, work, roles):
    # a worktree's are read on GitHub: its clone is known only from a `.git` file the agent run
    # could have changed
    path = checkout(ident, o, n, work=work)
    git = os.path.join(path, ".git")
    if os.path.isdir(git):
        # The agent run can write this clone's config; these keep it from running code as the
        # harness.
        res = run(
            [
                "git",
                *GUARD,
                "-C",
                path,
                f"--git-dir={git}",
                "branch",
                "--list",
                f"{ident}-*",
                "--format=%(refname:short)",
            ],
            SHORT,
        )
        if res.returncode != 0:
            return Transient(f"git branch --list: {err_text(res)}")
        read_only = {
            f"{ident}-{t}"
            for r, role in roles.items()
            if TASKS[r].kind != "build"
            for t in role.tasks
        }
        if local := [b for b in res.stdout.split() if b not in read_only]:
            return local
    res = run(
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "ls-remote",
            "--heads",
            f"https://github.com/{o}/{n}.git",
            f"{ident}-*",
        ],
        SHORT,
    )
    if res.returncode != 0:
        return Transient(f"git ls-remote: {err_text(res)}")
    return [
        line.split("refs/heads/", 1)[1] for line in res.stdout.splitlines() if "refs/heads/" in line
    ]


def check(issue, repos, *, run=None, work, roles) -> Target | Invalid | Transient:
    if run is None:
        run = config.shell
    repo = parse_repo(issue.description)
    mapped = repo == NO_LINE and issue.project_id in repos
    tag = MAPPED if mapped else ""
    if mapped:
        repo = repo_slug(repos[issue.project_id]) or Invalid(
            f"{MAPPED}{str(repos[issue.project_id])[:80]!r}: not <owner>/<name>", mapped=True
        )
    if isinstance(repo, Invalid):
        return repo
    asked = f"{tag}{repo[0]}/{repo[1]}"
    info = _repo_info(*repo, run)
    if isinstance(info, Invalid):
        return Invalid(tag + info.reason, mapped=mapped)
    if isinstance(info, Transient):
        return info
    (o, n), can_push, default = info
    if not can_push:
        return Invalid(f"{asked}: no push permission", mapped=mapped)
    if not isinstance(default, str) or not REF.fullmatch(default):
        return Invalid(f"{asked}: unsafe default branch name", mapped=mapped)
    ident = issue.identifier
    names = _branches(ident, o, n, run=run, work=work, roles=roles)
    if isinstance(names, Transient):
        return names
    if len(names) > 1:
        return Invalid(f"several {ident}-* branches: {', '.join(names)[:200]}")
    if names and not re.fullmatch(rf"{re.escape(ident)}-[a-z0-9-]{{1,40}}", names[0]):
        return Invalid(f"existing branch name {names[0][:80]!r} is not {ident}-<lowercase slug>")
    return Target(o, n, names[0] if names else f"{ident}-{slug(issue.title)}")


def with_clone(t, ident, *, run=None, work, clones, writable=None) -> Target:
    if run is None:
        run = config.shell
    # the checkout is agent-writable, so a clone its `.git` file names counts only outside
    # `writable` and with the right origin
    path = checkout(ident, t.owner, t.name, work=work)
    git, full = os.path.join(path, ".git"), f"{t.owner}/{t.name}"
    if os.path.isdir(git):
        return replace(t, clone="")
    if common := common_dir(path):
        clone = os.path.dirname(common)
        roots = config.writable(work) if writable is None else writable
        if not under(common, roots) and config.clone_error(clone, full, run=run) is None:
            return replace(t, clone=clone)
    return replace(t, clone=next((v for k, v in clones.items() if k.lower() == full.lower()), ""))
