import json
import subprocess
import urllib.request
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.data import VIEWER, refused
from orchestrator_test_helpers.unit.linear.linear_gql_fixtures import LinearGql as LinearGqlBase


class LinearGql1(LinearGqlBase):
    def test_key_by_service_only_sent_by_urlopen(self):
        for service in ("svc-h", "linear-api-key-pm"):
            with self.subTest(service=service):
                out, run, urlopen = self.gql(
                    body=b'{"data": {"viewer": {"id": "v"}}}', service=service
                )
                self.assertEqual(out, {"viewer": {"id": "v"}})
                self.assertEqual(
                    [c.args[0] for c in run.call_args_list],
                    [["/usr/bin/security", "find-generic-password", "-s", service, "-w"]],
                )
                urlopen.assert_called_once()
                req = urlopen.call_args[0][0]
                self.assertEqual(
                    (req.full_url, req.get_header("Authorization")),
                    ("https://api.linear.app/graphql", "secret"),
                )
                self.assertEqual(json.loads(req.data)["variables"], {})

    def test_timeout_bounds_keychain_and_request(self):
        for kw, want in (({}, 30), ({"timeout": 5}, 5)):
            with self.subTest(timeout=want):
                _, run, urlopen = self.gql(
                    "query($i: String!) { issue(id: $i) { id } }", i="TASK-1", service="svc", **kw
                )
                self.assertEqual(run.call_args.kwargs["timeout"], want)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], want)
                self.assertEqual(
                    json.loads(urlopen.call_args[0][0].data)["variables"], {"i": "TASK-1"}
                )

    def test_the_service_is_required(self):
        with (
            mock.patch.object(subprocess, "run", side_effect=AssertionError("Keychain read")),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("request sent")
            ),
            self.assertRaises(TypeError),
        ):
            linear.linear_gql(VIEWER)
        self.assertFalse(hasattr(linear, "harness_service"))

    def test_a_bad_keychain_key_raises_naming_only_the_service(self):
        for out, key in (("\n", ""), ("lin api\n", "lin api"), ("lin\x1bapi\n", "lin\x1bapi")):
            with (
                self.subTest(key=key),
                mock.patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=out)),
            ):
                code = refused(self, lambda: linear.linear_gql(VIEWER, service="svc"))
                self.assertIn("svc", code)
                if key:
                    self.assertNotIn(key, code)
