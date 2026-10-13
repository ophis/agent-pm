"""Shared fixtures for repo behavior."""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock

from agent_pm.delegate.composer import TEAM
from agent_pm.delegate.team.tools.types import RepoServices

from delegate.tests.case import Case

LOCAL = "config.local.toml"


SHA = "a" * 40


URL = "https://github.com/o/n.git"


SPEC = importlib.util.spec_from_file_location("repo", os.path.join(TEAM, "tools", "repo.py"))


repo = importlib.util.module_from_spec(SPEC)


SPEC.loader.exec_module(repo)


FETCH = ("fetch", "--refmap=", "origin", "+refs/heads/*:refs/remotes/origin/*")


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(stderr):
    return subprocess.CompletedProcess([], 1, "", stderr)


class Fake:
    """A `run` that answers by argv prefix (a list answers in turn) and records every call; `gh repo
    clone` makes the checkout's .git.
    """

    def __init__(self, answers):
        self.answers, self.calls, self.timeouts = answers, [], []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if argv[:3] == ["gh", "repo", "clone"]:
            os.makedirs(os.path.join(argv[4], ".git"))
        for prefix, res in self.answers:
            if prefix(argv) if callable(prefix) else argv[: len(prefix)] == prefix:
                return res.pop(0) if isinstance(res, list) else res
        return ok()

    def ran(self, *prefix):
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)

    def timeout(self, *prefix):
        return next(
            t
            for c, t in zip(self.calls, self.timeouts, strict=False)
            if c[: len(prefix)] == list(prefix)
        )


class Real(Fake):
    """Real git; `gh` as in Fake; an origin's URL reads as URL."""

    def __call__(self, argv, timeout):
        if argv[0] != "git":
            return super().__call__(argv, timeout)
        self.calls.append(argv)
        self.timeouts.append(timeout)
        for prefix, res in self.answers:
            if callable(prefix) and prefix(argv):
                return res
        res = repo.sh(argv, timeout)
        if argv[-3:] == ["remote", "get-url", "origin"] and res.returncode == 0:
            return ok(URL + "\n")
        return res


def info(*, push=True, default="main"):
    return (
        ["gh", "api", "--hostname"],
        ok(json.dumps({"permissions": {"push": push}, "default_branch": default})),
    )


HEAD = (lambda a: a[3:] == ["rev-parse", "HEAD"], ok(SHA + "\n"))


LOGIN = (["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}'))


NO_FEEDBACK = (lambda a: "--paginate" in a, ok("[[]]"))


REPO_API = ("gh", "api", "--hostname", "github.com", "repos/o/n")


def pr_list(rows):
    return (["gh", "pr", "list"], ok(json.dumps(rows)))


def pr(base="main"):
    """The branch's own PR 7."""
    return {
        "number": 7,
        "url": "u7",
        "state": "OPEN",
        "baseRefName": base,
        "isCrossRepository": False,
        "author": {"login": "me"},
    }


def git(*argv):
    return subprocess.run(["git", *argv], check=True, capture_output=True, text=True).stdout.strip()


def upstream(wt):
    res = subprocess.run(
        ["git", "-C", wt, "rev-parse", "--abbrev-ref", "@{upstream}"],
        capture_output=True,
        text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else None


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.wt = os.path.join(self.dir, "o", "n-TASK-1-x")
        self.none = os.devnull

    def existing(self, url=URL):
        os.makedirs(os.path.join(self.wt, ".git"), exist_ok=True)
        return (lambda a: a[3:] == ["remote", "get-url", "origin"], ok(url + "\n"))

    def main(self, argv, run, config=None, local=None):
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(
            argv,
            config=config or self.none,
            local=local or self.none,
            services=RepoServices(run=run, out=out, err=err),
        )
        return code, out.getvalue(), err.getvalue()

    def config(self, text):
        path = os.path.join(self.dir, "config.toml")
        with open(path, "w") as f:
            f.write(text)
        return path


class Clone(Case):
    """A real clone of a bare repo, its origin reading as github.com/o/n."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        env = unittest.mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.seed, self.bare, self.clone = (
            os.path.join(self.tmp, d) for d in ("seed", "origin.git", "clone")
        )
        git("init", "-q", "-b", "main", self.seed)
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "one")
        git("-C", self.seed, "branch", "TASK-1-remote")
        git("clone", "-q", "--bare", self.seed, self.bare)
        git("-C", self.seed, "remote", "add", "origin", self.bare)
        git("clone", "-q", self.bare, self.clone)
        self.dir = os.path.join(self.tmp, "work", "src")
        self.wt = os.path.join(self.dir, "o", "n-TASK-1-x")
        self.none = os.devnull

    def listing(self, *paths):
        """A config.toml whose trusted_dirs lists `paths`."""
        path = os.path.join(self.tmp, "config.toml")
        with open(path, "w") as f:
            f.write(f"trusted_dirs = {json.dumps(paths)}\n")
        return path

    def advance(self):
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "two")
        git("-C", self.seed, "push", "-q", "origin", "main")
        return git("-C", self.seed, "rev-parse", "HEAD")
