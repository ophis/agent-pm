#!/usr/bin/env python3
"""Target repos for agent runs: one checkout per repo at DIR/<owner>/<name>-<SLUG> (checkout(), the one checkout path
rule for any caller), on the run's branch. Also read_config, the one config loader every reader uses. Stdlib only,
importing nothing from src/.

repo.py worktree --dir DIR --branch B [--name SLUG] REPO
    prints {"repo", "host", "default", "branch", "commit", "worktree", "permalink_base", "push"}; no push permission is
    no error
repo.py status --dir DIR --branch B [--name SLUG] REPO
    after worktree with the same SLUG: {"pr", "plan_docs", "since", "user", "others"}, `user` and `others` being PR
    comments and reviews by the user (core config's `users`, else the gh login) and by anyone else since the latest plan
    doc commit
SLUG: 1-100 of [A-Za-z0-9._-], default B with / → -.
REPO is `owner/name`, `host/owner/name`, `https://host/owner/name` or a local clone's path (/ or ~), named by its origin
and outside the temp dirs. A local clone gets a git worktree after a fetch that moves only `origin/*`; any other REPO a
blobless clone. Symlinks follow config.toml's `trusted_dirs` comment, set in the worktree's own config (which turns on
the clone's extensions.worktreeConfig); an existing worktree whose setting disagrees is refused: remove it, run again.
B: the local B, else tracking origin/B, else new from origin/<default>.
Each option may be given once, so a command pre-approved by its `--dir` prefix can't be redirected elsewhere by a
second `--dir`.
Exits 2 when REPO, B or SLUG is invalid or unusable (B the default branch or checked out elsewhere), 1 on any other
failure.
Errors mask URL userinfo.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
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
URL = re.compile(r"(?:(https?|ssh)://(?:[^@/\s]+@)?|[^@/:\s]+@)([^/:\s]+)(?::(\d{1,5}))?[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?")
FETCH = ("fetch", "--refmap=", "origin", "+refs/heads/*:refs/remotes/origin/*")
USERINFO = re.compile(r"//[^@/\s]+@")
LOCK = re.compile(r"(?:cannot|could not) lock|\.lock\b", re.I)
BRANCH = re.compile(r"(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])")
SLUG = re.compile(r"[A-Za-z0-9._-]{1,100}")
SHORT, LONG = 60, 600
GUARD = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.toml")
LOCAL = "~/.agent-pm/core.local.toml"

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
    """host/owner/name of a clone URL as written, or None; an ssh port is no part of the web host."""
    m = URL.fullmatch(url.strip())
    if not m:
        return None
    scheme, host, port, owner, name = m.groups()
    return f"{host}:{port}/{owner}/{name}" if port and scheme in ("http", "https") else f"{host}/{owner}/{name}"


def origin(url: str) -> str | None:
    """host/owner/name of a clone URL, lowercased, or None."""
    slug = url_slug(url)
    return slug.lower() if slug else None


def redact(text: str) -> str:
    """`text` with URL userinfo (a token in an origin URL) masked."""
    return USERINFO.sub("//***@", text)


def err_text(res: subprocess.CompletedProcess) -> str:
    return redact((res.stderr or "").strip())[:200]


def merge(base: dict, over: dict) -> dict:
    """`base` with `over` on top: tables merge key by key; any other value of `over` replaces base's."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_config(path: str, local: str = LOCAL) -> dict:
    """TOML file `path`, with TOML file `local` (~ expanded) merged on top when it exists (merge())."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local = os.path.expanduser(local)
    if os.path.isfile(local):
        with open(local, "rb") as f:
            cfg = merge(cfg, tomllib.load(f))
    return cfg


def trusted_dirs(cfg: dict) -> frozenset[str]:
    """Realpaths of the config's `trusted_dirs`; ValueError unless it is a list of absolute or expandable ~ paths."""
    paths = cfg.get("trusted_dirs", [])
    if not isinstance(paths, list):
        raise ValueError("trusted_dirs: want a list of local paths")
    for p in paths:
        if not isinstance(p, str) or not p.startswith(("/", "~")) or os.path.expanduser(p).startswith("~"):
            raise ValueError(f"trusted_dirs: {p!r:.80} is not an absolute or expandable ~ path")
    return frozenset(os.path.realpath(os.path.expanduser(p)) for p in paths)


def status_line(cfg: dict) -> bool:
    """The global `status_line`: whether a TUI session shows tmux's status line; unset → off."""
    v = cfg.get("status_line", False)
    if not isinstance(v, bool):
        raise ValueError("status_line: want true or false")
    return v


def temp_dirs() -> tuple[str, ...]:
    """Realpaths of the dirs any process, an agent run included, may write: TMPDIR, Python's temp dir, /tmp, /var/tmp
    and macOS's per-user one."""
    dirs = [os.environ.get("TMPDIR", ""), tempfile.gettempdir(), "/tmp", "/var/tmp"]
    try:
        dirs.append(subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, timeout=SHORT,
                                   stdin=subprocess.DEVNULL).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return tuple(dict.fromkeys(os.path.realpath(d) for d in dirs if d))


def under(path: str, roots) -> str | None:
    """The first of `roots` that holds realpath `path`, or None."""
    return next((r for r in map(os.path.realpath, roots) if os.path.commonpath([path, r]) == r), None)


def git(run: Runner, wt: str, *args: str, timeout: int = SHORT, pre: tuple[str, ...] = ()) -> str:
    """Retries a lock failure: runs may share a clone."""
    for i in range(5):
        res = run(["git", *pre, "-C", wt, *args], timeout)
        if res.returncode == 0:
            return res.stdout
        if i == 4 or not LOCK.search(res.stderr or ""):
            break
        time.sleep(0.5 * 2**i)
    shown = " ".join(a if len(a) <= 40 else a[:37] + "..." for a in args)
    raise RuntimeError(f"git {shown[:200]}: {err_text(res)}")


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
    """(repo, local clone or None) of a REPO argument; Invalid if unreadable."""
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


def check_slug(slug: str | None, branch: str = "") -> str:
    """The checkout slug: `slug`, else `branch` with / → -; Invalid unless a SLUG."""
    slug = branch.replace("/", "-") if slug is None else slug
    if not SLUG.fullmatch(slug):
        raise Invalid(f"unsafe checkout name {slug[:80]!r}")
    return slug


def checkout(base: str, owner: str, name: str, slug: str) -> str:
    """The checkout path of repo owner/name under `base`; Invalid unless `slug` is a SLUG."""
    return os.path.join(os.path.abspath(base), owner, f"{name}-{check_slug(slug)}")


def place(repo: Repo, base: str, clone: str | None, slug: str, *, run: Runner) -> tuple[str, bool]:
    """(worktree, whether it exists); Invalid if it holds anything but a clone of the repo or a worktree of `clone`."""
    wt = checkout(base, repo.owner, repo.name, slug)
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


def registered(run: Runner, at: str) -> dict[str, str | None]:
    """{realpath: branch or None} of the worktrees registered in `at`'s clone, existing or not."""
    found, path = {}, None
    for line in git(run, at, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = os.path.realpath(line[len("worktree "):])
            found[path] = None
        elif line.startswith("branch refs/heads/") and path:
            found[path] = line[len("branch refs/heads/"):]
    return found


def worktree(spec: str, branch: str, base: str, *, slug: str | None = None, run: Runner = sh, temp=None,
             links=frozenset()) -> dict:
    """The checkout's details, on `branch`; raises Invalid, or RuntimeError on other failures. `temp`: dirs a local
    clone may not live in (default temp_dirs()), since prune treats their clones as untrusted. `links`: realpaths of the
    local clones whose worktrees get symlinks (trusted_dirs(), matched exactly)."""
    check_branch(branch)
    slug = check_slug(slug, branch)
    repo, clone = resolve(spec, run=run)
    if clone and (d := under(clone, temp_dirs() if temp is None else temp)):
        raise Invalid(f"{clone} is under the temp dir {d}: keep local clones outside temp dirs")
    symlinks = "true" if clone is not None and clone in links else "false"
    wt, exists = place(repo, base, clone, slug, run=run)
    if exists and clone and os.path.isfile(os.path.join(wt, ".git")):
        index = git(run, wt, "rev-parse", "--path-format=absolute", "--git-path", "index").strip()
        if not os.path.isfile(index):   # its checkout never finished: it holds nothing
            git(run, clone, "worktree", "remove", "--force", wt, timeout=LONG)
            exists = False
        else:
            found = run(["git", "-C", wt, "config", "--worktree", "--get", "core.symlinks"], SHORT).stdout.strip()
            if found != symlinks:
                raise Invalid(f"{wt} has core.symlinks={found or 'unset'}, trusted_dirs gives {symlinks}: "
                              "remove it and run again")
    push, default = info(repo, run=run)
    if not isinstance(default, str) or not BRANCH.fullmatch(default):
        raise Invalid(f"{repo.slug}: unsafe default branch name")
    if branch == default:
        raise Invalid(f"{branch} is the default branch; build on another")
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
            opts, start = ["--no-track", "-b", branch], f"origin/{default}"
        if add:
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            # core.symlinks per worktree, so the clone's own checkout keeps its config. --force re-adds our own path
            # when it was deleted by hand but is still registered.
            if run(["git", "-C", clone, "config", "--get", "extensions.worktreeConfig"], SHORT).stdout.strip() != "true":
                git(run, clone, "config", "extensions.worktreeConfig", "true")
            force = ["--force"] if mine in trees else []
            git(run, clone, "worktree", "add", "--no-checkout", *force, *opts, wt, start, timeout=LONG)
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
    return {"repo": f"{repo.owner}/{repo.name}", "host": repo.host, "default": default, "branch": branch, "commit": commit,
            "worktree": wt, "permalink_base": f"https://{repo.slug}/blob/{commit}/", "push": push}


def common_dir(wt: str) -> str | None:
    """The `<clone>/.git` that worktree `wt`'s `.git` file names, found by reading files as git reads them (whole file,
    trailing CR/LF dropped), never by running git; None if it names none."""
    def read(path):
        if not os.path.isfile(path):   # a FIFO would block open()
            return None
        with open(path, "rb") as f:
            return f.read(4096).decode().rstrip("\r\n")
    try:
        dotgit = os.path.join(wt, ".git")
        text = None if os.path.islink(dotgit) else read(dotgit)
        if not text or not text.startswith("gitdir: "):
            return None
        gitdir = os.path.realpath(os.path.join(wt, text[len("gitdir: "):]))
        rel = read(os.path.join(gitdir, "commondir"))
        common = os.path.realpath(os.path.join(gitdir, rel)) if rel else None
    except (OSError, ValueError):   # UnicodeDecodeError and a NUL in a path are ValueErrors
        return None
    return common if common and os.path.basename(common) == ".git" and os.path.isdir(common) else None


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


def status(spec: str, branch: str, base: str, *, slug: str | None = None, run: Runner = sh,
           users: list[str] | None = None) -> dict:
    """The branch's PR, plan docs and PR feedback since the latest plan doc commit; raises Invalid or RuntimeError."""
    slug = check_slug(slug, branch)
    repo, clone = resolve(spec, run=run)
    wt, exists = place(repo, base, clone, slug, run=run)
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


def main(argv: list[str], run: Runner = sh, out=sys.stdout, err=sys.stderr, config: str = CONFIG, temp=None) -> int:
    ap = argparse.ArgumentParser(prog="repo.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("worktree", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("--dir", required=True, action=Once)
        p.add_argument("--branch", required=True, action=Once)
        p.add_argument("--name", action=Once)
        p.add_argument("repo")
    a = ap.parse_args(argv)
    try:
        cfg = read_config(config)
        if a.cmd == "worktree":
            r = worktree(a.repo, a.branch, a.dir, slug=a.name, run=run, temp=temp, links=trusted_dirs(cfg))
        else:
            r = status(a.repo, a.branch, a.dir, slug=a.name, run=run, users=cfg.get("users"))
    except Invalid as e:
        err.write(f"repo.py: {redact(str(e))}\n")
        return 2
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired) as e:
        err.write(f"repo.py: {redact(str(e))}\n")
        return 1
    json.dump(r, out)
    out.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
