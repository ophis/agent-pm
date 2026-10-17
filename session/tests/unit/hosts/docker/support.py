"""The broker protocol's shared fixtures and a fake broker runner."""

import json
import os
from types import SimpleNamespace

from session.tests.case import PLUGIN_BIN, REPO

FIXTURES = os.path.join(REPO, "protocol", "docker-broker", "fixtures")
HOOK = os.path.join(PLUGIN_BIN, "agent-pm-session")


def cases(prefix=""):
    with open(os.path.join(FIXTURES, "index.json")) as f:
        return [c for c in json.load(f)["cases"] if c["file"].startswith(prefix)]


def text(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def data(name):
    return json.loads(text(name))


class FakeBroker:
    """Plays the broker: each call pops the next (rc, stdout, stderr) or exception; records argv and
    keywords.
    """

    def __init__(self, *results):
        self.results, self.calls, self.kwargs = list(results), [], []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        rc, out, err = result
        return SimpleNamespace(args=argv, returncode=rc, stdout=out, stderr=err)


def success(name):
    return 0, text(name), ""


def failure(name, rc=None):
    found = data(name)
    code = found.get("exit_code")
    return (code if rc is None else rc), "", text(name)
