#!/usr/bin/env python3
"""Target repos for runs, checked out into DIR/<name>; a checkout of the same repo there is reused.

repo.py prepare --dir DIR REPO             read-only, shallow, detached at the default branch; prints
                                           {"repo", "host", "commit", "worktree", "permalink_base"}
repo.py checkout --dir DIR --branch B REPO  writable, on branch B (from origin/B, else the default branch); needs push
                                           permission; prints {"repo", "host", "default", "branch", "worktree"}
repo.py status --dir DIR --branch B REPO    after checkout: {"pr", "plan_docs", "user", "others"}, `user` and `others`
                                           being PR comments and reviews by the user (config.toml's `users`, else
                                           the gh login) and by anyone else since the latest plan doc commit
REPO is `owner/name`, `host/owner/name` or `https://host/owner/name`. Each option may be given once, so a command
pre-approved by its `--dir` prefix can't be redirected elsewhere by a second `--dir`.
Exits 2 when REPO or B is invalid or unreachable (not found, no access or push permission, DIR/<name> holds
something else), 1 on any other failure.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
NAME = r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}"
HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?"
SPEC = re.compile(rf"(?:https://)?(?:({HOST})/)?({OWNER})/({NAME}?)(?:\.git)?/?")
SHA = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(r"(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])")
SHORT, LONG = 60, 600
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.toml")

Runner = Callable[[list[str], int], subprocess.CompletedProcess]


class Invalid(Exception):
    pass


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{option_string} given twice")
        setattr(namespace, self.dest, values)


def sh(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


@dataclass(frozen=True)
class Repo:
    host: str
    owner: str
    name: str

    @classmethod
    def parse(cls, spec: str) -> "Repo":
        """The repo a REPO argument names; Invalid if unreadable."""
        m = SPEC.fullmatch(spec.strip())
        if not m or m.group(3) in (".", "..") or (m.group(1) is None and spec.strip().startswith("https://")):
            raise Invalid(f"unreadable repo {spec[:80]!r}: want owner/name, host/owner/name or https://host/owner/name")
        return cls((m.group(1) or "github.com").lower(), m.group(2), m.group(3))

    @property
    def slug(self) -> str:
        return f"{self.host}/{self.owner}/{self.name}"


def origin(url: str) -> str | None:
    """host/owner/name of a clone URL, lowercased, or None."""
    m = re.fullmatch(r"(?:https://|ssh://git@|git@)([^/:\s]+)[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?", url.strip())
    return "/".join(m.groups()).lower() if m else None


def _err(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()[:200]


def git(run: Runner, wt: str, *args: str, timeout: int = SHORT) -> str:
    res = run(["git", "-C", wt, *args], timeout)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)[:100]}: {_err(res)}")
    return res.stdout


def gh_json(run: Runner, argv: list[str]):
    res = run(["gh", *argv], SHORT)
    if res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv)[:100]}: {_err(res)}")
    try:
        return json.loads(res.stdout)
    except ValueError as e:
        raise RuntimeError(f"gh {' '.join(argv)[:100]}: {e}") from None


def info(repo: Repo, *, run: Runner) -> tuple[bool, str]:
    """(push permission, default branch) of the repo; Invalid if not found or no access."""
    res = run(["gh", "api", "--hostname", repo.host, f"repos/{repo.owner}/{repo.name}"], SHORT)
    if res.returncode != 0:
        code = re.search(r"HTTP (\d{3})", res.stderr or "")
        if code and code.group(1) in ("403", "404"):
            raise Invalid(f"{repo.slug}: not found or no access (HTTP {code.group(1)})")
        raise RuntimeError(f"gh api: {_err(res)}")
    try:
        data = json.loads(res.stdout)
        return data["permissions"]["push"] is True, data["default_branch"]
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"gh api repos/{repo.owner}/{repo.name}: {e!r}") from None


def place(repo: Repo, base: str, *, run: Runner) -> tuple[str, bool]:
    """(worktree, whether it exists) for the repo under `base`; Invalid if base/<name> holds anything but its checkout."""
    wt = os.path.join(os.path.abspath(base), repo.name)
    if not os.path.lexists(wt):
        return wt, False
    if os.path.islink(wt) or not os.path.isdir(os.path.join(wt, ".git")):
        raise Invalid(f"{wt} exists and is not a checkout")
    res = run(["git", "-C", wt, "remote", "get-url", "origin"], SHORT)
    if res.returncode != 0 or origin(res.stdout) != repo.slug.lower():
        raise Invalid(f"{wt} is not a checkout of {repo.slug}")
    return wt, True


def clone(repo: Repo, wt: str, *extra: str, run: Runner) -> None:
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    # The repo is untrusted: no symlinks that could point outside the checkout.
    res = run(["gh", "repo", "clone", repo.slug, wt, "--", "-c", "core.symlinks=false", *extra], LONG)
    if res.returncode != 0:
        raise RuntimeError(f"gh repo clone: {_err(res)}")


def prepare(repo: Repo, base: str, *, run: Runner = sh) -> dict:
    """The read-only checkout's details; raises Invalid, or RuntimeError on other failures."""
    wt, exists = place(repo, base, run=run)
    if not exists:
        info(repo, run=run)
        clone(repo, wt, "--depth", "1", run=run)
        git(run, wt, "checkout", "--detach")
    commit = git(run, wt, "rev-parse", "HEAD").strip()
    if not SHA.fullmatch(commit):
        raise RuntimeError(f"git rev-parse HEAD: {commit[:80]!r}")
    return {"repo": f"{repo.owner}/{repo.name}", "host": repo.host, "commit": commit, "worktree": wt,
            "permalink_base": f"https://{repo.slug}/blob/{commit}/"}


def check_branch(branch: str) -> None:
    if not BRANCH.fullmatch(branch):
        raise Invalid(f"unsafe branch name {branch[:80]!r}")


def checkout(repo: Repo, branch: str, base: str, *, run: Runner = sh) -> dict:
    """The writable checkout's details, on `branch`; raises Invalid, or RuntimeError on other failures."""
    check_branch(branch)
    wt, exists = place(repo, base, run=run)
    push, default = info(repo, run=run)
    if not push:
        raise Invalid(f"{repo.slug}: no push permission")
    if not isinstance(default, str) or not BRANCH.fullmatch(default):
        raise Invalid(f"{repo.slug}: unsafe default branch name")
    if branch == default:
        raise Invalid(f"{branch} is the default branch; build on another")
    if not exists:
        clone(repo, wt, run=run)
    else:
        git(run, wt, "fetch", "origin", timeout=LONG)
    if git(run, wt, "branch", "--show-current").strip() != branch:
        if git(run, wt, "branch", "--list", branch).strip():
            git(run, wt, "checkout", branch)
        elif git(run, wt, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip():
            git(run, wt, "checkout", "--track", "-b", branch, f"origin/{branch}")
        else:
            git(run, wt, "checkout", "--no-track", "-b", branch, f"origin/{default}")
    return {"repo": f"{repo.owner}/{repo.name}", "host": repo.host, "default": default, "branch": branch, "worktree": wt}


def plan_docs(wt: str, branch: str) -> list[dict]:
    """[{"path", "phase"}] of the autopilot plan docs for `branch` in the checkout."""
    found = []
    for parent, dirs, files in os.walk(wt):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".claude")]
        for f in files:
            if f.endswith(".md"):
                p = os.path.join(parent, f)
                try:
                    with open(p, errors="replace") as fh:
                        m = re.search(r"RESUME: phase=(S\d)([^\n]*)", fh.read())
                except OSError:
                    continue
                if m and f"branch={branch}" in m.group(2).split():
                    found.append({"path": p, "phase": m.group(1)})
    return sorted(found, key=lambda d: d["path"])


def _author(row: dict, key: str) -> str | None:
    who = row.get(key)
    return who.get("login") if isinstance(who, dict) else None


def _oldest_first(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: datetime.fromisoformat(e["at"]))


def status(repo: Repo, branch: str, base: str, *, run: Runner = sh, users: list[str] | None = None) -> dict:
    """The branch's PR, plan docs and PR feedback since the latest plan doc commit; raises Invalid or RuntimeError."""
    wt, exists = place(repo, base, run=run)
    if not exists:
        raise Invalid(f"{wt} missing: run checkout first")
    docs = plan_docs(wt, branch)
    since = git(run, wt, "log", "-1", "--format=%cI", "--", *[d["path"] for d in docs]).strip() if docs else ""
    login = gh_json(run, ["api", "--hostname", repo.host, "user"]).get("login")
    rows = gh_json(run, ["pr", "list", "--repo", repo.slug, "--head", branch, "--state", "all",
                         "--json", "number,url,state,isCrossRepository,author", "--limit", "100"])
    mine = [r for r in rows if r.get("isCrossRepository") is False and _author(r, "author") == login]
    by_user = {u.lower() for u in users} if users else {login.lower()}
    pr = {k: mine[0].get(k) for k in ("number", "url", "state")} if mine else None
    user, others = [], []
    if pr:
        api = f"repos/{repo.owner}/{repo.name}"
        for path, key, kind in ((f"{api}/issues/{pr['number']}/comments", "created_at", "comment"),
                                (f"{api}/pulls/{pr['number']}/reviews", "submitted_at", "review"),
                                (f"{api}/pulls/{pr['number']}/comments", "created_at", "review_comment")):
            for page in gh_json(run, ["api", "--hostname", repo.host, "--paginate", "--slurp", path]):
                for c in page:
                    at = c.get(key)
                    if at is None or (since and datetime.fromisoformat(at) <= datetime.fromisoformat(since)):
                        continue
                    e = {"at": at, "kind": kind, "author": _author(c, "user"), "body": c.get("body") or ""}
                    if kind == "review":
                        e["state"] = c.get("state")
                    if kind == "review_comment":
                        e["path"], e["line"] = c.get("path"), c.get("line") or c.get("original_line")
                    (user if (e["author"] or "").lower() in by_user else others).append(e)
    return {"pr": pr, "plan_docs": docs, "since": since or None, "user": _oldest_first(user), "others": _oldest_first(others)}


def main(argv: list[str], run: Runner = sh, out=sys.stdout, err=sys.stderr, config: str = CONFIG) -> int:
    ap = argparse.ArgumentParser(prog="repo.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("prepare", "checkout", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("--dir", required=True, action=Once)
        if cmd != "prepare":
            p.add_argument("--branch", required=True, action=Once)
        p.add_argument("repo")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "prepare":
            r = prepare(Repo.parse(a.repo), a.dir, run=run)
        elif a.cmd == "checkout":
            check_branch(a.branch)
            r = checkout(Repo.parse(a.repo), a.branch, a.dir, run=run)
        else:
            users = None
            if os.path.isfile(config):   # a skill's copy of this script has no config.toml beside it
                with open(config, "rb") as f:
                    users = tomllib.load(f).get("users")
            r = status(Repo.parse(a.repo), a.branch, a.dir, run=run, users=users)
    except Invalid as e:
        err.write(f"repo.py: {e}\n")
        return 2
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired) as e:
        err.write(f"repo.py: {e}\n")
        return 1
    json.dump(r, out)
    out.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
