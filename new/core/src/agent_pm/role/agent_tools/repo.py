#!/usr/bin/env python3
"""Target repos for agent runs: one checkout per repo at DIR/<owner>/<name>-<SLUG>, on the run's branch.

repo.py worktree --dir DIR --branch B [--name SLUG] [--base BASE] REPO
    prints {"repo", "host", "default", "branch", "commit", "worktree", "permalink_base", "push"}; no push permission is
    no error
repo.py status --dir DIR --branch B [--name SLUG] [--base BASE] REPO
    after worktree with the same SLUG: {"pr", "base", "plan_docs", "since", "user", "others"}, `pr` being B's PR
    {"number", "url", "state", "baseRefName"} or null, `base` the PR's baseRefName, else BASE, else the default branch,
    `user` and `others` PR comments and reviews by the user (core config's `users`, else the gh login) and by anyone
    else since the latest plan doc commit
SLUG: 1-100 of [A-Za-z0-9._-], default B with / → -.
REPO is `owner/name`, `host/owner/name`, `https://host/owner/name` or a local clone's path (/ or ~), named by its origin
and outside the temp dirs. A local clone gets a git worktree after a fetch that moves only `origin/*`; any other REPO a
blobless clone. Symlinks: the core config's trusted_dirs comment. B: the local B, else tracking origin/B, else new from
origin/BASE (origin/<default> when BASE is omitted).
Each option may be given once, so a command pre-approved by its `--dir` prefix can't be redirected elsewhere by a
second `--dir`.
Exits 2 when REPO, B or SLUG is invalid or unusable (B the default branch or checked out elsewhere), or BASE syntax
is invalid. Exits 1 on other failures, including a missing origin/BASE.
Errors mask URL userinfo.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime

from agent_pm.config import CONFIG, LOCAL
from agent_pm.role.repos import (  # noqa: F401
    HOST,
    NAME,
    OWNER,
    SLUG,
    SPEC,
    URL,
    Invalid,
    Repo,
    check_slug,
    checkout,
    origin,
    resolve,
    url_slug,
)
from agent_pm.role.repos.git import GUARD, LOCK, common_dir, git  # noqa: F401
from agent_pm.role.repos.shell import (  # noqa: F401
    LONG,
    SHORT,
    USERINFO,
    Runner,
    err_text,
    redact,
    sh,
)
from agent_pm.utils.args import Once
from agent_pm.utils.config_file import read_config
from agent_pm.utils.paths import real_dirs, temp_dirs, under

SHA = re.compile(r"[0-9a-f]{40}")
FETCH = ("fetch", "--refmap=", "origin", "+refs/heads/*:refs/remotes/origin/*")
BRANCH = re.compile(r"(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])")


def trusted_dirs(cfg: dict) -> frozenset[str]:
    return real_dirs(cfg.get("trusted_dirs", []), "trusted_dirs")


def gh_json(run: Runner, argv: list[str]):
    res = run(["gh", *argv], SHORT)
    if res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv)[:100]}: {err_text(res)}")
    try:
        return json.loads(res.stdout)
    except ValueError as e:
        raise RuntimeError(f"gh {' '.join(argv)[:100]}: {e}") from None


def info(repo: Repo, *, run: Runner) -> tuple[bool, str]:
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


def place(repo: Repo, base: str, clone: str | None, slug: str, *, run: Runner) -> tuple[str, bool]:
    wt = checkout(base, repo.owner, repo.name, slug)
    if not os.path.lexists(wt):
        return wt, False
    dotgit = os.path.join(wt, ".git")
    if (
        os.path.islink(wt)
        or os.path.islink(dotgit)
        or not (os.path.isdir(dotgit) or os.path.isfile(dotgit))
    ):
        raise Invalid(f"{wt} exists and is not a checkout")
    if os.path.isfile(dotgit):
        res = (
            run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"], SHORT)
            if clone
            else None
        )
        if (
            res is None
            or res.returncode != 0
            or os.path.realpath(res.stdout.strip()) != os.path.join(clone, ".git")
        ):
            raise Invalid(f"{wt} is not a checkout of {repo.slug}")
    res = run(["git", "-C", wt, "remote", "get-url", "origin"], SHORT)
    if res.returncode != 0 or origin(res.stdout) != repo.slug.lower():
        raise Invalid(f"{wt} is not a checkout of {repo.slug}")
    return wt, True


def clone_repo(repo: Repo, wt: str, *, run: Runner) -> None:
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    # The repo is untrusted: no symlinks that could point outside the checkout.
    res = run(
        [
            "gh",
            "repo",
            "clone",
            repo.slug,
            wt,
            "--",
            "-c",
            "core.symlinks=false",
            "--filter=blob:none",
        ],
        LONG,
    )
    if res.returncode != 0:
        raise RuntimeError(f"gh repo clone: {err_text(res)}")


def check_branch(branch: str) -> None:
    if not BRANCH.fullmatch(branch):
        raise Invalid(f"unsafe branch name {branch[:80]!r}")


def registered(run: Runner, at: str) -> dict[str, str | None]:
    found, path = {}, None
    for line in git(run, at, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = os.path.realpath(line[len("worktree ") :])
            found[path] = None
        elif line.startswith("branch refs/heads/") and path:
            found[path] = line[len("branch refs/heads/") :]
    return found


def worktree(
    spec: str,
    branch: str,
    base: str,
    *,
    slug: str | None = None,
    run: Runner = sh,
    temp=None,
    links=frozenset(),
    base_branch: str | None = None,
) -> dict:
    # a local clone in a temp dir is refused: prune treats clones there as untrusted
    check_branch(branch)
    slug = check_slug(slug, branch)
    repo, clone = resolve(spec, run=run)
    if clone and (d := under(clone, temp_dirs() if temp is None else temp)):
        raise Invalid(f"{clone} is under the temp dir {d}: keep local clones outside temp dirs")
    symlinks = "true" if clone is not None and clone in links else "false"
    wt, exists = place(repo, base, clone, slug, run=run)
    if exists and clone and os.path.isfile(os.path.join(wt, ".git")):
        index = git(run, wt, "rev-parse", "--path-format=absolute", "--git-path", "index").strip()
        if not os.path.isfile(index):  # its checkout never finished: it holds nothing
            git(run, clone, "worktree", "remove", "--force", wt, timeout=LONG)
            exists = False
        else:
            found = run(
                ["git", "-C", wt, "config", "--worktree", "--get", "core.symlinks"], SHORT
            ).stdout.strip()
            if found != symlinks:
                raise Invalid(
                    f"{wt} has core.symlinks={found or 'unset'}, trusted_dirs gives {symlinks}: "
                    "remove it and run again"
                )
    push, default = info(repo, run=run)
    if not isinstance(default, str) or not BRANCH.fullmatch(default):
        raise Invalid(f"{repo.slug}: unsafe default branch name")
    if branch == default:
        raise Invalid(f"{branch} is the default branch; build on another")
    onto = base_branch if base_branch is not None else default
    if not isinstance(onto, str) or not BRANCH.fullmatch(onto):
        raise Invalid(f"unsafe base branch name {str(onto)[:80]!r}")
    add = clone is not None and not exists
    # An explicit refspec without the configured map: a mirror-style remote.origin.fetch can't move local branches.
    if exists:
        git(run, wt, *FETCH, timeout=LONG)
    elif add:
        git(run, clone, *FETCH, timeout=LONG)
    else:
        clone_repo(repo, wt, run=run)
    at = clone if add else wt
    if add or git(run, wt, "branch", "--show-current").strip() != branch:
        trees, mine = registered(run, at), os.path.realpath(wt)
        busy = [p for p, b in trees.items() if b == branch and p != mine]
        if busy:
            raise Invalid(f"{branch} is checked out in {busy[0]}")
        if git(run, at, "branch", "--list", branch).strip():
            opts, start = [], branch
        elif git(run, at, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip():
            opts, start = ["--track", "-b", branch], f"origin/{branch}"
        else:
            opts, start = ["--no-track", "-b", branch], f"origin/{onto}"
        if add:
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            # core.symlinks per worktree, so the clone's own checkout keeps its config. --force re-adds our own path
            # when it was deleted by hand but is still registered.
            if (
                run(
                    ["git", "-C", clone, "config", "--get", "extensions.worktreeConfig"], SHORT
                ).stdout.strip()
                != "true"
            ):
                git(run, clone, "config", "extensions.worktreeConfig", "true")
            force = ["--force"] if mine in trees else []
            git(
                run,
                clone,
                "worktree",
                "add",
                "--no-checkout",
                *force,
                *opts,
                wt,
                start,
                timeout=LONG,
            )
            try:
                git(run, wt, "config", "--worktree", "core.symlinks", symlinks)
                git(run, wt, "reset", "-q", "--hard", timeout=LONG)
            except (RuntimeError, subprocess.TimeoutExpired):
                run(["git", "-C", clone, "worktree", "remove", "--force", wt], LONG)
                raise
        else:
            git(run, wt, "checkout", *opts, start, timeout=LONG)
    commit = git(run, wt, "rev-parse", "HEAD").strip()
    if not SHA.fullmatch(commit):
        raise RuntimeError(f"git rev-parse HEAD: {commit[:80]!r}")
    return {
        "repo": f"{repo.owner}/{repo.name}",
        "host": repo.host,
        "default": default,
        "branch": branch,
        "commit": commit,
        "worktree": wt,
        "permalink_base": f"https://{repo.slug}/blob/{commit}/",
        "push": push,
    }


def plan_docs(wt: str, branch: str) -> list[dict]:
    found = []
    for parent, dirs, files in os.walk(wt):
        dirs[:] = [
            d
            for d in dirs
            if d not in (".git", "node_modules")
            and not os.path.lexists(os.path.join(parent, d, ".git"))
        ]
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


def status(
    spec: str,
    branch: str,
    base: str,
    *,
    slug: str | None = None,
    run: Runner = sh,
    users: list[str] | None = None,
    base_branch: str | None = None,
) -> dict:
    slug = check_slug(slug, branch)
    repo, clone = resolve(spec, run=run)
    wt, exists = place(repo, base, clone, slug, run=run)
    if not exists:
        raise Invalid(f"{wt} missing: run worktree first")
    docs = plan_docs(wt, branch)
    since = (
        git(run, wt, "log", "-1", "--format=%cI", "--", *[d["path"] for d in docs]).strip()
        if docs
        else ""
    )
    login = gh_json(run, ["api", "--hostname", repo.host, "user"]).get("login")
    rows = gh_json(
        run,
        [
            "pr",
            "list",
            "--repo",
            repo.slug,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,url,state,baseRefName,isCrossRepository,author",
            "--limit",
            "100",
        ],
    )
    mine = [
        r for r in rows if r.get("isCrossRepository") is False and _author(r, "author") == login
    ]
    by_user = {u.lower() for u in users} if users else {login.lower()}
    pr = {k: mine[0].get(k) for k in ("number", "url", "state", "baseRefName")} if mine else None
    onto = (
        pr["baseRefName"]
        if pr
        else base_branch
        if base_branch is not None
        else info(repo, run=run)[1]
    )
    if not isinstance(onto, str) or not BRANCH.fullmatch(onto):
        raise Invalid(f"unsafe base branch name {str(onto)[:80]!r}")
    user, others = [], []
    if pr:
        api = f"repos/{repo.owner}/{repo.name}"
        for path, key, kind in (
            (f"{api}/issues/{pr['number']}/comments", "created_at", "comment"),
            (f"{api}/pulls/{pr['number']}/reviews", "submitted_at", "review"),
            (f"{api}/pulls/{pr['number']}/comments", "created_at", "review_comment"),
        ):
            for page in gh_json(
                run, ["api", "--hostname", repo.host, "--paginate", "--slurp", path]
            ):
                for c in page:
                    at = c.get(key)
                    if at is None or (
                        since and datetime.fromisoformat(at) <= datetime.fromisoformat(since)
                    ):
                        continue
                    e = {
                        "at": at,
                        "kind": kind,
                        "author": _author(c, "user"),
                        "body": c.get("body") or "",
                    }
                    if kind == "review":
                        e["state"] = c.get("state")
                    if kind == "review_comment":
                        e["path"], e["line"] = (
                            c.get("path"),
                            c.get("line") or c.get("original_line"),
                        )
                    (user if (e["author"] or "").lower() in by_user else others).append(e)
    return {
        "pr": pr,
        "base": onto,
        "plan_docs": docs,
        "since": since or None,
        "user": _oldest_first(user),
        "others": _oldest_first(others),
    }


def main(
    argv: list[str],
    run: Runner = sh,
    out=sys.stdout,
    err=sys.stderr,
    config: str = CONFIG,
    local: str = LOCAL,
    temp=None,
) -> int:
    ap = argparse.ArgumentParser(prog="repo.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("worktree", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("--dir", required=True, action=Once)
        p.add_argument("--branch", required=True, action=Once)
        p.add_argument("--name", action=Once)
        p.add_argument("--base", action=Once)
        p.add_argument("repo")
    a = ap.parse_args(argv)
    try:
        cfg = read_config(config, local)
        if a.cmd == "worktree":
            r = worktree(
                a.repo,
                a.branch,
                a.dir,
                slug=a.name,
                run=run,
                temp=temp,
                links=trusted_dirs(cfg),
                base_branch=a.base,
            )
        else:
            r = status(
                a.repo,
                a.branch,
                a.dir,
                slug=a.name,
                run=run,
                users=cfg.get("users"),
                base_branch=a.base,
            )
    except Invalid as e:
        err.write(f"repo.py: {redact(str(e))}\n")
        return 2
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        subprocess.TimeoutExpired,
    ) as e:
        err.write(f"repo.py: {redact(str(e))}\n")
        return 1
    json.dump(r, out)
    out.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
