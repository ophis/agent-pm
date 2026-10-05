"""The target repo of an issue: `Repo:` line or project mapping, the engineering pre-check, branch name, PR title."""
import json
import os
import re
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CLONES, NAME, OWNER, REF, TASKS, WORK, agent_writable, repo_slug, sh_run  # noqa: E402
from repo import GUARD, SHORT, Invalid as NoCheckout, Untrusted, clone_of, err_text  # noqa: E402

MAPPED = "project mapping "


@dataclass(frozen=True)
class Target:
    owner: str
    name: str
    branch: str = ""


@dataclass(frozen=True)
class Invalid:
    reason: str


@dataclass(frozen=True)
class Transient:
    reason: str


def _one(value):
    v = value.strip()
    m = re.fullmatch(r"\[[^\]]*\]\(<?([^)>]+)>?\)", v)
    if m:
        v = m.group(1).strip()
    for pat in (rf"https://github\.com/({OWNER})/({NAME}?)(?:\.git)?/?", rf"git@github\.com:({OWNER})/({NAME}?)(?:\.git)?"):
        m = re.fullmatch(pat, v)
        if m and m.group(2) not in (".", ".."):
            return m.group(1), m.group(2)
    return repo_slug(v)


def parse_repo(description) -> tuple[str, str] | Invalid:
    found = []
    for line in (description or "").splitlines():
        if line.rstrip("\r") == "## Comments":
            break
        # Markdown a pasted line may carry: a list or quote marker, `code`, **bold**.
        m = re.match(r"^\s*(?:[-*>]\s+)?(?:\*\*|`)?repo:(?:\*\*)?\s*(.+?)\s*$", line.rstrip("\r"), re.I)
        if m:
            one = _one(m.group(1).strip("`*").strip())
            if not one:
                return Invalid(f"unreadable Repo line: {m.group(1)[:80]!r}")
            found.append(one)
    distinct = {(o.lower(), n.lower()) for o, n in found}
    if not found:
        return Invalid("no Repo: line in the description or its ## Instructions")
    if len(distinct) > 1:
        return Invalid("several different Repo: values")
    return found[0]


NO_LINE = parse_repo("")


def slug(title):
    t = re.sub(r"^[A-Z][A-Z0-9]*: ", "", title, count=1).lower()
    s = "-".join(re.findall(r"[a-z0-9]+", t))[:40].rstrip("-")
    return s or "build"


def pr_title(ident, title):
    """`<ID>: <title without prefix>` reduced to Unicode letters/digits, spaces and .,:()_/- (safe in single quotes)."""
    t = f"{ident}: " + re.sub(r"^[A-Z][A-Z0-9]*: ", "", title, count=1)
    return " ".join(re.sub(r"[^\w .,:()/-]", " ", t).split())


def research_repo(issue, repos) -> Target | None:
    """The project's mapped repo when the description has no `Repo:` line, else None."""
    if parse_repo(issue.description) != NO_LINE or issue.project_id not in repos:
        return None
    one = repo_slug(repos[issue.project_id])
    return Target(*one) if one else None


def _repo_info(o, n, run):
    """(full_name's (owner, name), push permission, default branch) from `gh api repos/…`, or an Invalid/Transient."""
    res = run(["gh", "api", f"repos/{o}/{n}"], SHORT)
    if res.returncode != 0:
        code = re.search(r"HTTP (\d{3})", res.stderr or "")
        if code and code.group(1) in ("403", "404"):
            return Invalid(f"{o}/{n}: not found or no access (HTTP {code.group(1)})")
        return Transient(f"gh api repos/{o}/{n}: {err_text(res)}")
    try:
        data = json.loads(res.stdout)
        full, push, default = repo_slug(data["full_name"]), data["permissions"]["push"], data["default_branch"]
    except (ValueError, KeyError, TypeError):
        full = None
    if not full:
        return Transient(f"gh api repos/{o}/{n}: unusable response")
    return full, push is True, default


def _branches(ident, o, n, run, work, untrusted):
    """Names of the <ident>-* branches but read-only tasks' `<ident>-<task>`: in the agent run's own checkout, else on
    GitHub (gh's credentials, no shared clone); or a Transient. A worktree's are read in its clone, only when clone_of
    finds one outside `untrusted`."""
    checkout = os.path.join(work, ident, CLONES[0], o, n)
    git, at = os.path.join(checkout, ".git"), None
    if os.path.isdir(git) and not os.path.islink(git):
        at = checkout
    elif os.path.isfile(git) and not os.path.islink(git):
        try:
            at = clone_of(checkout, untrusted)[0]
        except (NoCheckout, Untrusted):
            pass
    if at:
        # The agent run can write a checkout's config; these keep it from running code as the harness.
        res = run(["git", *GUARD, "-C", at, f"--git-dir={os.path.join(at, '.git')}",
                   "branch", "--list", f"{ident}-*", "--format=%(refname:short)"], SHORT)
        if res.returncode != 0:
            return Transient(f"git branch --list: {err_text(res)}")
        read_only = {f"{ident}-{t}" for t, task in TASKS.items() if task.kind != "build"}
        if local := [b for b in res.stdout.split() if b not in read_only]:
            return local
    res = run(["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
               "ls-remote", "--heads", f"https://github.com/{o}/{n}.git", f"{ident}-*"], SHORT)
    if res.returncode != 0:
        return Transient(f"git ls-remote: {err_text(res)}")
    return [line.split("refs/heads/", 1)[1] for line in res.stdout.splitlines() if "refs/heads/" in line]


def check(issue, repos, *, run=sh_run, work=WORK, untrusted=None) -> Target | Invalid | Transient:
    """The engineering pre-check: the repo is reachable with push permission and a safe default branch; its branch name."""
    repo = parse_repo(issue.description)
    mapped = repo == NO_LINE and issue.project_id in repos
    tag = MAPPED if mapped else ""
    if mapped:
        repo = repo_slug(repos[issue.project_id]) or Invalid(f"{MAPPED}{str(repos[issue.project_id])[:80]!r}: not <owner>/<name>")
    if isinstance(repo, Invalid):
        return repo
    asked = f"{tag}{repo[0]}/{repo[1]}"
    info = _repo_info(*repo, run)
    if isinstance(info, Invalid):
        return Invalid(tag + info.reason)
    if isinstance(info, Transient):
        return info
    (o, n), can_push, default = info
    if not can_push:
        return Invalid(f"{asked}: no push permission")
    if not isinstance(default, str) or not REF.fullmatch(default):
        return Invalid(f"{asked}: unsafe default branch name")
    ident = issue.identifier
    names = _branches(ident, o, n, run, work, agent_writable(work) if untrusted is None else untrusted)
    if isinstance(names, Transient):
        return names
    if len(names) > 1:
        return Invalid(f"several {ident}-* branches: {', '.join(names)[:200]}")
    if names and not re.fullmatch(rf"{re.escape(ident)}-[a-z0-9-]{{1,40}}", names[0]):
        return Invalid(f"existing branch name {names[0][:80]!r} is not {ident}-<lowercase slug>")
    return Target(o, n, names[0] if names else f"{ident}-{slug(issue.title)}")
