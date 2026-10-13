"""Loopback HTTP transport and fake credential process for integration subprocesses."""

import contextlib
import json
import os
import subprocess
import urllib.parse
import urllib.request
from unittest import mock

ENV = "FAKE_LINEAR_TRANSPORT"


def install(path):
    run = subprocess.run
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def settings():
        with open(os.environ.get(ENV, path), encoding="utf-8") as stream:
            value = json.load(stream)
        url = urllib.parse.urlsplit(value["url"])
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1") or url.port is None:
            raise ValueError("fake transport requires a loopback HTTP endpoint")
        return value

    def send(request, *, timeout):
        if request.full_url != "https://api.linear.app/graphql":
            raise ValueError("fake transport refuses an unexpected endpoint")
        value = settings()
        redirected = urllib.request.Request(
            value["url"], data=request.data, headers=dict(request.header_items())
        )
        return opener.open(redirected, timeout=timeout)

    def process(argv, *args, **kwargs):
        if not argv or argv[0] != "/usr/bin/security":
            return run(argv, *args, **kwargs)
        if (
            len(argv) not in (4, 5)
            or list(argv[1:3]) != ["find-generic-password", "-s"]
            or (len(argv) == 5 and argv[4] != "-w")
        ):
            raise ValueError("fake transport refuses an unexpected credential command")
        key = settings()["keys"].get(argv[3])
        result = subprocess.CompletedProcess(
            argv,
            0 if key is not None else 44,
            (key + "\n") if key is not None and len(argv) == 5 else "",
            "",
        )
        if kwargs.get("check") and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, argv)
        return result

    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(urllib.request, "urlopen", send))
    stack.enter_context(mock.patch.object(subprocess, "run", process))
    return stack
