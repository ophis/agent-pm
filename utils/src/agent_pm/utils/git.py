"""GUARD: git options disabling fsmonitor and hooks; git: retries lock failures; common_dir: a
worktree's clone. Repository specs, origin URLs and checkout paths identify and locate repos.
"""

import os
import re
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from agent_pm.utils.shell import SHORT, Runner, err_text

GUARD = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
LOCK = re.compile(r"(?:cannot|could not) lock|\.lock\b", re.I)
LOCAL_ENV_VARS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
    }
)


@dataclass(frozen=True)
class Checkouts:
    """Inner-to-outer checkout roots; error prevents treating partial discovery as complete."""

    roots: tuple[str, ...]
    error: str | None = None


def find_checkouts(
    path: str,
    *,
    runner: Callable[[list[str], int, dict[str, str]], subprocess.CompletedProcess[str]],
    timeout: int,
) -> Checkouts:
    """Find every containing checkout; runner(argv, timeout, env) runs only on invocation."""
    roots = []
    env = {k: v for k, v in os.environ.items() if k not in LOCAL_ENV_VARS}
    env["LC_ALL"] = "C"
    try:
        current = _existing_directory(os.path.realpath(os.path.expanduser(path)))
        while True:
            marker = False
            try:
                os.lstat(os.path.join(current, ".git"))
                marker = True
            except FileNotFoundError:
                pass
            result = runner(
                [
                    "git",
                    *GUARD,
                    "-C",
                    current,
                    "rev-parse",
                    "--is-inside-work-tree",
                    "--show-toplevel",
                ],
                timeout,
                env,
            )
            inside, _, top = result.stdout.partition("\n")
            if result.returncode == 0:
                top = top.removesuffix("\n")
                if inside != "true" or not os.path.isabs(top):
                    return Checkouts(tuple(roots), f"{current}: invalid git rev-parse output")
                root = os.path.realpath(top)
                if marker and not os.path.samefile(root, current):
                    return Checkouts(tuple(roots), f"{current}: invalid .git metadata")
                if not any(os.path.samefile(root, previous) for previous in roots):
                    roots.append(root)
            elif not (
                not marker
                and result.returncode == 128
                and (
                    result.stderr.startswith("fatal: not a git repository")
                    or inside == "false"
                    and result.stderr.strip() == "fatal: this operation must be run in a work tree"
                )
            ):
                return Checkouts(tuple(roots), f"{current}: {err_text(result)}")
            parent = os.path.dirname(current)
            if parent == current:
                return Checkouts(tuple(roots))
            current = parent
    except (OSError, subprocess.SubprocessError) as error:
        return Checkouts(tuple(roots), f"{path}: {error}")


def _existing_directory(path: str) -> str:
    while True:
        try:
            if stat.S_ISDIR(os.stat(path).st_mode):
                return path
        except (FileNotFoundError, NotADirectoryError):
            pass
        parent = os.path.dirname(path)
        if parent == path:
            raise FileNotFoundError(path)
        path = parent


class GitError(RuntimeError):
    def __init__(self, args, result):
        shown = " ".join(a if len(a) <= 40 else a[:37] + "..." for a in args)
        super().__init__(f"git {shown[:200]}: {err_text(result)}")


def git(wt: str, *args: str, run: Runner, timeout: int = SHORT, pre: tuple[str, ...] = ()) -> str:
    for i in range(5):
        res = run(["git", *pre, "-C", wt, *args], timeout)
        if res.returncode == 0:
            return res.stdout
        if i == 4 or not LOCK.search(res.stderr or ""):
            break
        time.sleep(0.5 * 2**i)
    raise GitError(args, res)


def common_dir(wt: str) -> str | None:
    # never runs git: reads the files as git does (whole file, trailing CR/LF dropped)
    def read(path, *, follow=True):
        flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            with os.fdopen(fd, "rb", closefd=False) as f:
                return f.read(4096).decode().rstrip("\r\n")
        finally:
            os.close(fd)

    try:
        dotgit = os.path.join(wt, ".git")
        text = read(dotgit, follow=False)
        if not text or not text.startswith("gitdir: "):
            return None
        gitdir = os.path.realpath(os.path.join(wt, text[len("gitdir: ") :]))
        rel = read(os.path.join(gitdir, "commondir"))
        common = os.path.realpath(os.path.join(gitdir, rel)) if rel else None
    except (OSError, ValueError):  # UnicodeDecodeError and a NUL in a path are ValueErrors
        return None
    return (
        common if common and os.path.basename(common) == ".git" and os.path.isdir(common) else None
    )


OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
NAME = r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}"
HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?"
SPEC = re.compile(rf"(?:https://)?(?:({HOST})/)?({OWNER})/({NAME}?)(?:\.git)?/?")
URL = re.compile(
    r"(?:(https?|ssh)://(?:[^@/\s]+@)?|[^@/:\s]+@)([^/:\s]+)(?::(\d{1,5}))?[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?"
)
SLUG = re.compile(r"[A-Za-z0-9._-]{1,100}")


def repo_slug(value) -> tuple[str, str] | None:
    match = re.fullmatch(rf"({OWNER})/({NAME})", value) if isinstance(value, str) else None
    return (match.group(1), match.group(2)) if match and match.group(2) not in (".", "..") else None


class Invalid(Exception):
    pass


@dataclass(frozen=True)
class Repo:
    host: str
    owner: str
    name: str

    @classmethod
    def parse(cls, spec: str) -> "Repo":
        m = SPEC.fullmatch(spec.strip())
        if (
            not m
            or m.group(3) in (".", "..")
            or (m.group(1) is None and spec.strip().startswith("https://"))
        ):
            raise Invalid(
                f"unreadable repo {spec[:80]!r}: want owner/name, host/owner/name or https://host/owner/name"
            )
        return cls((m.group(1) or "github.com").lower(), m.group(2), m.group(3))

    @property
    def slug(self) -> str:
        return f"{self.host}/{self.owner}/{self.name}"


def url_slug(url: str) -> str | None:
    # an ssh port is no part of the web host
    m = URL.fullmatch(url.strip())
    if not m:
        return None
    scheme, host, port, owner, name = m.groups()
    return (
        f"{host}:{port}/{owner}/{name}"
        if port and scheme in ("http", "https")
        else f"{host}/{owner}/{name}"
    )


def origin(url: str) -> str | None:
    slug = url_slug(url)
    return slug.lower() if slug else None


def resolve(spec: str, *, run: Runner) -> tuple[Repo, str | None]:
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
    slug = branch.replace("/", "-") if slug is None else slug
    if not SLUG.fullmatch(slug):
        raise Invalid(f"unsafe checkout name {slug[:80]!r}")
    return slug


def checkout(base: str, owner: str, name: str, slug: str) -> str:
    return os.path.join(os.path.abspath(base), owner, f"{name}-{check_slug(slug)}")
