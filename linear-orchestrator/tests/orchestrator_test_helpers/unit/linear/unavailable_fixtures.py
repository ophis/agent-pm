import subprocess
import urllib.request
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.linear.data import SECRET, respond


class Unavailable(Case):
    def setUp(self):
        p = mock.patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=SECRET + "\n"))
        p.start()
        self.addCleanup(p.stop)

    def gql(self, query, opened=None, read=None, body=b'{"data": {}}'):
        resp = respond(body)
        if read:
            resp.__enter__.return_value = mock.Mock(read=mock.Mock(side_effect=read))
        with mock.patch.object(urllib.request, "urlopen", side_effect=opened, return_value=resp):
            return linear.linear_gql(query, service="svc")

    def unavailable(self, query, **kw):
        with self.assertRaises(linear.Unavailable) as cm:
            self.gql(query, **kw)
        e = cm.exception
        for text in (str(e), linear.one_line(e)):
            self.assertNotIn(SECRET, text)
        return e.op, e.status, e.reason, str(e)
