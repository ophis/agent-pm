#!/usr/bin/env python3
"""Target repos for runs, checked out into DIR/<name>; a checkout of the same repo there is reused.

repo.py prepare REPO --dir DIR             read-only, shallow, detached at the default branch; prints
                                           {"repo", "host", "commit", "worktree", "permalink_base"}
repo.py checkout REPO --branch B --dir DIR  writable, on branch B (from origin/B, else the default branch); needs push
                                           permission; prints {"repo", "host", "default", "branch", "worktree"}
repo.py status REPO --branch B --dir DIR    after checkout: {"pr", "plan_docs", "user", "others"}, `user` and `others`
                                           being PR comments and reviews by the user (config.toml's `users`, else
                                           the gh login) and by anyone else since the latest plan doc commit
REPO is `owner/name`, `host/owner/name` or `https://host/owner/name`.
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
from datetime import datetime

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
NAME = r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}"
HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?"
SPEC = re.compile(rf"(?:https://)?(?:({HOST})/)?({OWNER})/({NAME}?)(?:\.git)?/?")
SHA = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(r"(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])")
SHORT, LONG = 60, 600
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.toml")


class Invalid(Exception):
    pass


def sh(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def parse(spec):
    """(host, owner, name) of a REPO argument; Invalid if unreadable."""
    m = SPEC.fullmatch(spec.strip())
    if not m or m.group(3) in (".", "..") or (m.group(1) is None and spec.strip().startswith("https://")):
        raise Invalid(f"unreadable repo {spec[:80]!r}: want owner/name, host/owner/name or https://host/owner/name")
    return (m.group(1) or "github.com").lower(), m.group(2), m.group(3)


def origin(url):
    """host/owner/name of a clone URL, lowercased, or None."""
    m = re.fullmatch(r"(?:https://|ssh://git@|git@)([^/:\s]+)[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?", url.strip())
    return "/".join(m.groups()).lower() if m else None


def _err(res):
    return (res.stderr or "").strip()[:200]


def git(run, wt, *args, timeout=SHORT):
    res = run(["git", "-C", wt, *args], timeout)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)[:100]}: {_err(res)}")
    return res.stdout


def gh_json(run, argv):
    res = run(["gh", *argv], SHORT)
    if res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv)[:100]}: {_err(res)}")
    try:
        return json.loads(res.stdout)
    except ValueError as e:
        raise RuntimeError(f"gh {' '.join(argv)[:100]}: {e}") from None


def info(run, host, owner, name):
    """(push permission, default branch) of the repo; Invalid if not found or no access."""
    res = run(["gh", "api", "--hostname", host, f"repos/{owner}/{name}"], SHORT)
    if res.returncode != 0:
        code = re.search(r"HTTP (\d{3})", res.stderr or "")
        if code and code.group(1) in ("403", "404"):
            raise Invalid(f"{host}/{owner}/{name}: not found or no access (HTTP {code.group(1)})")
        raise RuntimeError(f"gh api: {_err(res)}")
    try:
        data = json.loads(res.stdout)
        return data["permissions"]["push"] is True, data["default_branch"]
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"gh api repos/{owner}/{name}: {e!r}") from None


def place(spec, dir, run):
    """(host, owner, name, worktree, exists) for REPO under DIR; Invalid if DIR/<name> holds anything but its checkout."""
    host, owner, name = parse(spec)
    wt = os.path.join(os.path.abspath(dir), name)
    if not os.path.lexists(wt):
        return host, owner, name, wt, False
    if os.path.islink(wt) or not os.path.isdir(os.path.join(wt, ".git")):
        raise Invalid(f"{wt} exists and is not a checkout")
    res = run(["git", "-C", wt, "remote", "get-url", "origin"], SHORT)
    if res.returncode != 0 or origin(res.stdout) != f"{host}/{owner}/{name}".lower():
        raise Invalid(f"{wt} is not a checkout of {host}/{owner}/{name}")
    return host, owner, name, wt, True


def clone(run, host, owner, name, wt, *extra):
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    # The repo is untrusted: no symlinks that could point outside the checkout.
    res = run(["gh", "repo", "clone", f"{host}/{owner}/{name}", wt, "--", "-c", "core.symlinks=false", *extra], LONG)
    if res.returncode != 0:
        raise RuntimeError(f"gh repo clone: {_err(res)}")


def prepare(spec, dir, run=sh):
    """The read-only checkout's details; raises Invalid, or RuntimeError on other failures."""
    host, owner, name, wt, exists = place(spec, dir, run)
    if not exists:
        info(run, host, owner, name)
        clone(run, host, owner, name, wt, "--depth", "1")
        git(run, wt, "checkout", "--detach")
    commit = git(run, wt, "rev-parse", "HEAD").strip()
    if not SHA.fullmatch(commit):
        raise RuntimeError(f"git rev-parse HEAD: {commit[:80]!r}")
    return {"repo": f"{owner}/{name}", "host": host, "commit": commit, "worktree": wt,
            "permalink_base": f"https://{host}/{owner}/{name}/blob/{commit}/"}


def checkout(spec, branch, dir, run=sh):
    """The writable checkout's details, on `branch`; raises Invalid, or RuntimeError on other failures."""
    if not BRANCH.fullmatch(branch):
        raise Invalid(f"unsafe branch name {branch[:80]!r}")
    host, owner, name, wt, exists = place(spec, dir, run)
    push, default = info(run, host, owner, name)
    if not push:
        raise Invalid(f"{host}/{owner}/{name}: no push permission")
    if not isinstance(default, str) or not BRANCH.fullmatch(default):
        raise Invalid(f"{host}/{owner}/{name}: unsafe default branch name")
    if not exists:
        clone(run, host, owner, name, wt)
    else:
        git(run, wt, "fetch", "origin", timeout=LONG)
    if git(run, wt, "branch", "--show-current").strip() != branch:
        if git(run, wt, "branch", "--list", branch).strip():
            git(run, wt, "checkout", branch)
        elif git(run, wt, "ls-remote", "--heads", "origin", branch).strip():
            git(run, wt, "checkout", "--track", "-b", branch, f"origin/{branch}")
        else:
            git(run, wt, "checkout", "--no-track", "-b", branch, f"origin/{default}")
    return {"repo": f"{owner}/{name}", "host": host, "default": default, "branch": branch, "worktree": wt}


def plan_docs(wt, branch):
    """[{"path", "phase"}] of the autopilot plan docs for `branch` in the checkout."""
    found = []
    for base, dirs, files in os.walk(wt):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".claude")]
        for f in files:
            if f.endswith(".md"):
                p = os.path.join(base, f)
                try:
                    with open(p, errors="replace") as fh:
                        m = re.search(r"RESUME: phase=(S\d)([^\n]*)", fh.read())
                except OSError:
                    continue
                if m and f"branch={branch}" in m.group(2).split():
                    found.append({"path": p, "phase": m.group(1)})
    return sorted(found, key=lambda d: d["path"])


def _author(row, key):
    who = row.get(key)
    return who.get("login") if isinstance(who, dict) else None


def status(spec, branch, dir, run=sh, users=None):
    """The branch's PR, plan docs and PR feedback since the latest plan doc commit; raises Invalid or RuntimeError."""
    host, owner, name, wt, exists = place(spec, dir, run)
    if not exists:
        raise Invalid(f"{wt} missing: run checkout first")
    docs = plan_docs(wt, branch)
    since = git(run, wt, "log", "-1", "--format=%cI", "--", *[d["path"] for d in docs]).strip() if docs else ""
    login = gh_json(run, ["api", "--hostname", host, "user"]).get("login")
    repo = f"{host}/{owner}/{name}"
    rows = gh_json(run, ["pr", "list", "--repo", repo, "--head", branch, "--state", "all",
                         "--json", "number,url,state,isCrossRepository,author", "--limit", "100"])
    mine = [r for r in rows if r.get("isCrossRepository") is False and _author(r, "author") == login]
    users = {u.lower() for u in users} if users else {login.lower()}
    pr = {k: mine[0].get(k) for k in ("number", "url", "state")} if mine else None
    user, others = [], []
    if pr:
        base = f"repos/{owner}/{name}"
        for path, key, kind in ((f"{base}/issues/{pr['number']}/comments", "created_at", "comment"),
                                (f"{base}/pulls/{pr['number']}/reviews", "submitted_at", "review"),
                                (f"{base}/pulls/{pr['number']}/comments", "created_at", "review_comment")):
            for page in gh_json(run, ["api", "--hostname", host, "--paginate", "--slurp", path]):
                for c in page:
                    at = c.get(key)
                    if at is None or (since and datetime.fromisoformat(at) <= datetime.fromisoformat(since)):
                        continue
                    e = {"at": at, "kind": kind, "author": _author(c, "user"), "body": c.get("body") or ""}
                    if kind == "review":
                        e["state"] = c.get("state")
                    if kind == "review_comment":
                        e["path"], e["line"] = c.get("path"), c.get("line") or c.get("original_line")
                    (user if (e["author"] or "").lower() in users else others).append(e)
    oldest_first = lambda rows: sorted(rows, key=lambda e: datetime.fromisoformat(e["at"]))
    return {"pr": pr, "plan_docs": docs, "since": since or None, "user": oldest_first(user), "others": oldest_first(others)}


def main(argv, run=sh, out=sys.stdout, err=sys.stderr, config=CONFIG):
    ap = argparse.ArgumentParser(prog="repo.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("prepare", "checkout", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("repo")
        p.add_argument("--dir", required=True)
        if cmd != "prepare":
            p.add_argument("--branch", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "prepare":
            r = prepare(a.repo, a.dir, run)
        elif a.cmd == "checkout":
            r = checkout(a.repo, a.branch, a.dir, run)
        else:
            users = None
            if os.path.isfile(config):   # a skill's copy of this script has no config.toml beside it
                with open(config, "rb") as f:
                    users = tomllib.load(f).get("users")
            r = status(a.repo, a.branch, a.dir, run, users=users)
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
