"""Repository tool fake results and fixtures."""

import importlib.util
import io
import json
import os
import subprocess
import tempfile

from agent_pm.delegate.composer import TEAM

from delegate.tests.case import Case

LOCAL = "config.local.toml"


SHA = "a" * 40


URL = "https://github.com/o/n.git"


SPEC = importlib.util.spec_from_file_location("repo", os.path.join(TEAM, "tools", "repo.py"))


repo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repo)


FETCH = ("fetch", "--refmap=", "origin", "+refs/heads/*:refs/remotes/origin/*")
PROCESS_ERRORS = (OSError("gh unavailable"), subprocess.TimeoutExpired(["gh", "api"], 60))


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
            run=run,
            out=out,
            err=err,
        )
        return code, out.getvalue(), err.getvalue()

    def config(self, text):
        path = os.path.join(self.dir, "config.toml")
        with open(path, "w") as f:
            f.write(text)
        return path
