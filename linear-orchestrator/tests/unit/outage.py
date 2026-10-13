import io
import os
import subprocess
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear

FAILURES = {  # factories: a fresh exception per use
    "5xx": lambda: urllib.error.HTTPError(linear.URL, 503, "Service Unavailable", {}, io.BytesIO()),
    "timeout": lambda: TimeoutError("timed out"),
    "URLError": lambda: urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
}
FIELDS = {
    "5xx": "status=503",
    "timeout": "reason=TimeoutError: timed out",
    "URLError": "reason=ConnectionRefusedError: [Errno 61] Connection refused",
}


def failing(error, gql=None, ops=None):
    failed = []

    def call(query, **v):
        if ops is not None and linear.operation(query) not in ops:
            return gql(query, **v)
        failed.append((query, v))
        with (
            mock.patch.dict(os.environ),
            mock.patch.object(urllib.request, "urlopen", side_effect=error()),
            mock.patch.object(
                subprocess, "run", return_value=SimpleNamespace(stdout="lin_api_outage\n")
            ),
        ):
            return linear.linear_gql(query, **{"service": "linear-api-key", **v})

    call.failed = failed
    return call
