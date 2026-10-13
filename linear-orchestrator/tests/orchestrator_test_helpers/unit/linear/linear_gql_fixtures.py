import subprocess
import urllib.request
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.linear.data import VIEWER, respond


class LinearGql(Case):
    def gql(self, query=VIEWER, body=b'{"data": {}}', **kw):
        run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
        with (
            mock.patch.object(subprocess, "run", run),
            mock.patch.object(
                urllib.request, "build_opener", side_effect=AssertionError("opener built")
            ),
            mock.patch.object(urllib.request, "urlopen", return_value=respond(body)) as urlopen,
        ):
            return linear.linear_gql(query, **kw), run, urlopen
