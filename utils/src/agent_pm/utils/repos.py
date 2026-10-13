"""Repo specs, origin URLs and checkout paths."""

import os
import re
from dataclasses import dataclass

from agent_pm.utils.shell import SHORT, Runner

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
NAME = r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}"
HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?"
SPEC = re.compile(rf"(?:https://)?(?:({HOST})/)?({OWNER})/({NAME}?)(?:\.git)?/?")
URL = re.compile(
    r"(?:(https?|ssh)://(?:[^@/\s]+@)?|[^@/:\s]+@)([^/:\s]+)(?::(\d{1,5}))?"
    r"[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?"
)
SLUG = re.compile(r"[A-Za-z0-9._-]{1,100}")


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
