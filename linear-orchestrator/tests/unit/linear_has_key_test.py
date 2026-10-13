import subprocess
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.has_key_fixtures import HasKey as HasKeyBase


class HasKey1(HasKeyBase):
    def test_key_existence_check_never_reads_the_secret(self):
        for code, want in ((0, True), (44, False)):
            with mock.patch.object(
                subprocess, "run", return_value=SimpleNamespace(returncode=code)
            ) as m:
                self.assertIs(linear.has_key("svc"), want)
            self.assertEqual(
                m.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s", "svc"]
            )
