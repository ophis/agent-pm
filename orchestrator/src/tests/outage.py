"""A Linear outage for the entry points' tests: the real linear.linear_gql, its transport failing."""
import io
import os
import sys
import urllib.error
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
import linear  # noqa: E402

FAILURES = {  # factories: a fresh exception per use
    "5xx": lambda: urllib.error.HTTPError(linear.URL, 503, "Service Unavailable", {}, io.BytesIO()),
    "timeout": lambda: TimeoutError("timed out"),
    "URLError": lambda: urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
}


def failing(error, gql=None, ops=None):
    """A gql: a query whose operation (linear.operation) is in ops, any when None, goes to the real linear_gql with
    urlopen raising error() (a FAILURES factory), the seam unset and the Keychain faked; any other to gql. Its .failed
    records those calls as (query, variables)."""
    failed = []

    def call(query, **v):
        if ops is not None and linear.operation(query) not in ops:
            return gql(query, **v)
        failed.append((query, v))
        with mock.patch.dict(os.environ), mock.patch.object(linear.urllib.request, "urlopen", side_effect=error()), \
                mock.patch.object(linear.subprocess, "run", return_value=SimpleNamespace(stdout="lin_api_outage\n")):
            os.environ.pop(linear.SEAM, None)
            # A service: harness_service() would read the real config.
            return linear.linear_gql(query, **{"service": "linear-api-key", **v})
    call.failed = failed
    return call
