#!/usr/bin/env python3
"""Target repos for agent runs: one checkout per repo at DIR/<owner>/<name>, on the run's own branch.

repo.py worktree --dir DIR --branch B REPO  the checkout on branch B; prints {"repo", "host", "default", "branch",
                                           "commit", "worktree", "permalink_base", "push"}, `push` being whether
                                           the gh user may push (no permission is no error)
repo.py status --dir DIR --branch B REPO    after worktree: {"pr", "plan_docs", "since", "user", "others"}, `user` and
                                           `others` being PR comments and reviews by the user (config.toml's `users`,
                                           else the gh login) and by anyone else since the latest plan doc commit
REPO is `owner/name`, `host/owner/name`, `https://host/owner/name`, or a local clone's path (starting with / or ~;
its .git a directory, its origin URL naming host/owner/name). From a local clone, `worktree` fetches origin (only
`origin/*` moves, never the clone's branches or work tree) and adds a git worktree; otherwise it clones blobless
(full history, blobs fetched on demand) with core.symlinks=false. A checkout already at DIR/<owner>/<name> is
reused (fetched only): a clone of the repo, or a worktree of the local clone REPO names.
Branch B: the local B, else a new B tracking origin/B, else a new B from origin/<default>; B may be neither the
default branch nor checked out in another worktree. Git calls that fail on a lock (several runs on one clone)
are retried: 5 attempts, 7.5 s of backoff in all.
Each option may be given once, so a command pre-approved by its `--dir` prefix can't be redirected elsewhere by a
second `--dir`.
Exits 2 when REPO or B is invalid or unusable (not found or no access, not a clone, no origin, DIR/<owner>/<name>
holds something else, B is the default branch or checked out elsewhere), 1 on any other failure.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
NAME = r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}"
HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?"
SPEC = re.compile(rf"(?:https://)?(?:({HOST})/)?({OWNER})/({NAME}?)(?:\.git)?/?")
SHA = re.compile(r"[0-9a-f]{40}")
URL = re.compile(r"(?:https://|ssh://git@|git@)([^/:\s]+)[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?")
LOCK = re.compile(r"(?:cannot|could not) lock|\.lock\b", re.I)
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


def url_slug(url: str) -> str | None:
    """host/owner/name of a clone URL as written, or None."""
    m = URL.fullmatch(url.strip())
    return "/".join(m.groups()) if m else None


def origin(url: str) -> str | None:
    """host/owner/name of a clone URL, lowercased, or None."""
    slug = url_slug(url)
    return slug.lower() if slug else None


def err_text(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()[:200]


def git(run: Runner, wt: str, *args: str, timeout: int = SHORT, pre: tuple[str, ...] = ()) -> str:
    """stdout of `git [pre] -C wt args`, retrying a lock failure; RuntimeError if it fails."""
    for i in range(5):
        res = run(["git", *pre, "-C", wt, *args], timeout)
        if res.returncode == 0:
            return res.stdout
        if i == 4 or not LOCK.search(res.stderr or ""):
            break
        time.sleep(0.5 * 2**i)
    raise RuntimeError(f"git {' '.join(args)[:100]}: {err_text(res)}")


def gh_json(run: Runner, argv: list[str]):
    res = run(["gh", *argv], SHORT)
    if res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv)[:100]}: {err_text(res)}")
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
        raise RuntimeError(f"gh api: {err_text(res)}")
    try:
        data = json.loads(res.stdout)
        return data["permissions"]["push"] is True, data["default_branch"]
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"gh api repos/{repo.owner}/{repo.name}: {e!r}") from None


def resolve(spec: str, *, run: Runner) -> tuple[Repo, str | None]:
    """(repo, local clone or None) of a REPO argument; Invalid if unreadable, or a path to no clone with an origin."""
    if not spec.startswith(("/", "~")):
        return Repo.parse(spec), None
    clone = os.path.realpath(os.path.expanduser(spec))
    dotgit = os.path.join(clone, ".git")
    if os.path.islink(dotgit) or not os.path.isdir(dotgit):
        raise Invalid(f"{clone} is not a git clone")
    res = run(["git", "-C", clone, "remote", "get-url", "origin"], SHORT)
    slug = url_slug(res.stdout) if res.returncode == 0 else None
    if slug is None:
        raise Invalid(f"{clone}: no origin URL naming host/owner/name")
    return Repo.parse(slug), clone


def place(repo: Repo, base: str, clone: str | None, *, run: Runner) -> tuple[str, bool]:
    """(worktree, whether it exists) for the repo under `base`; Invalid if base/<owner>/<name> holds anything but a
    clone of it or a worktree of `clone`."""
    wt = os.path.join(os.path.abspath(base), repo.owner, repo.name)
    if not os.path.lexists(wt):
        return wt, False
    dotgit = os.path.join(wt, ".git")
    if os.path.islink(wt) or os.path.islink(dotgit) or not (os.path.isdir(dotgit) or os.path.isfile(dotgit)):
        raise Invalid(f"{wt} exists and is not a checkout")
    if os.path.isfile(dotgit):
        res = run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"], SHORT) if clone else None
        if res is None or res.returncode != 0 or os.path.realpath(res.stdout.strip()) != os.path.join(clone, ".git"):
            raise Invalid(f"{wt} is not a checkout of {repo.slug}")
    res = run(["git", "-C", wt, "remote", "get-url", "origin"], SHORT)
    if res.returncode != 0 or origin(res.stdout) != repo.slug.lower():
        raise Invalid(f"{wt} is not a checkout of {repo.slug}")
    return wt, True


def clone_repo(repo: Repo, wt: str, *, run: Runner) -> None:
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    # The repo is untrusted: no symlinks that could point outside the checkout.
    res = run(["gh", "repo", "clone", repo.slug, wt, "--", "-c", "core.symlinks=false", "--filter=blob:none"], LONG)
    if res.returncode != 0:
        raise RuntimeError(f"gh repo clone: {err_text(res)}")


def check_branch(branch: str) -> None:
    if not BRANCH.fullmatch(branch):
        raise Invalid(f"unsafe branch name {branch[:80]!r}")


def check_free(run: Runner, at: str, branch: str, wt: str) -> None:
    """Invalid if `branch` is checked out in a worktree of `at` other than `wt`."""
    path = None
    for line in git(run, at, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path and os.path.realpath(path) != os.path.realpath(wt):
            raise Invalid(f"{branch} is checked out in {path}")


def worktree(spec: str, branch: str, base: str, *, run: Runner = sh) -> dict:
    """The checkout's details, on `branch`; raises Invalid, or RuntimeError on other failures."""
    check_branch(branch)
    repo, clone = resolve(spec, run=run)
    wt, exists = place(repo, base, clone, run=run)
    push, default = info(repo, run=run)
    if not isinstance(default, str) or not BRANCH.fullmatch(default):
        raise Invalid(f"{repo.slug}: unsafe default branch name")
    if branch == default:
        raise Invalid(f"{branch} is the default branch; build on another")
    add = clone is not None and not exists
    if exists:
        git(run, wt, "fetch", "origin", timeout=LONG)
    elif add:
        git(run, clone, "fetch", "origin", timeout=LONG)
    else:
        clone_repo(repo, wt, run=run)
    at = clone if add else wt
    if add or git(run, wt, "branch", "--show-current").strip() != branch:
        check_free(run, at, branch, wt)
        if git(run, at, "branch", "--list", branch).strip():
            opts, start = [], branch
        elif git(run, at, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip():
            opts, start = ["--track", "-b", branch], f"origin/{branch}"
        else:
            opts, start = ["--no-track", "-b", branch], f"origin/{default}"
        if add:
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            git(run, clone, "worktree", "add", *opts, wt, start)
        else:
            git(run, wt, "checkout", *opts, start)
    commit = git(run, wt, "rev-parse", "HEAD").strip()
    if not SHA.fullmatch(commit):
        raise RuntimeError(f"git rev-parse HEAD: {commit[:80]!r}")
    return {"repo": f"{repo.owner}/{repo.name}", "host": repo.host, "default": default, "branch": branch, "commit": commit,
            "worktree": wt, "permalink_base": f"https://{repo.slug}/blob/{commit}/", "push": push}


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


def status(spec: str, branch: str, base: str, *, run: Runner = sh, users: list[str] | None = None) -> dict:
    """The branch's PR, plan docs and PR feedback since the latest plan doc commit; raises Invalid or RuntimeError."""
    repo, clone = resolve(spec, run=run)
    wt, exists = place(repo, base, clone, run=run)
    if not exists:
        raise Invalid(f"{wt} missing: run worktree first")
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
    for cmd in ("worktree", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("--dir", required=True, action=Once)
        p.add_argument("--branch", required=True, action=Once)
        p.add_argument("repo")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "worktree":
            r = worktree(a.repo, a.branch, a.dir, run=run)
        else:
            users = None
            if os.path.isfile(config):   # a skill's copy of this script has no config.toml beside it
                with open(config, "rb") as f:
                    users = tomllib.load(f).get("users")
            r = status(a.repo, a.branch, a.dir, run=run, users=users)
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
