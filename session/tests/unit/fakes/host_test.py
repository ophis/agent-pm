"""session/tests/fakes/host.py: FakeHost's refusals, before any process starts (the rest: its
integration test).
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_pm.session import DriverError
from agent_pm.utils import ProcessControl

from session.tests.case import Case
from session.tests.contracts.host import SLEEP, program
from session.tests.fakes.host import FakeHost, FakeProcess


class FakeHostOnly(Case):
    def test_refuses_a_bad_name_argv_or_cwd(self):
        host = FakeHost("fake", {"root": "~/hosts"})
        good = (program(SLEEP), self.home, {})
        for name, argv, cwd in (
            ("a b", *good[:2]),
            ("../w", *good[:2]),
            ("", *good[:2]),
            ("w1", [], self.home),
            ("w1", "python3", self.home),
            ("w1", [sys.executable, 1], self.home),
            ("w1", good[0], os.path.join(self.home, "absent")),
        ):
            for kind in ("detach", "terminal"):
                with (
                    self.subTest(kind=kind, name=name, argv=argv, cwd=cwd),
                    self.assertRaises(ValueError),
                ):
                    getattr(host, kind)(name, argv, cwd, {})
        self.assertEqual(host.list(), [])


class ControlTest(Case):
    def process(self):
        folder = Path(self.home, "session")
        folder.mkdir()
        control = ProcessControl.prepare(["tool"], self.home)
        self.addCleanup(control.remove)
        Path(folder, "meta.json").write_text(json.dumps({"id": "first", "control": control.path}))
        return FakeProcess("mine", str(folder)), control

    def test_completed_generation_does_not_signal_after_unobserved_exit_or_replacement(self):
        process, control = self.process()
        Path(control.path, "done.json").write_text('{"rc": 0}')
        Path(process.folder, "meta.json").write_text('{"id": "replacement"}')
        with mock.patch("os.killpg") as killpg, mock.patch("socket.socket") as socket:
            process.kill()
            process.close()
            self.assertTrue(process.wait_exit(0))
            self.assertEqual(
                json.loads(Path(process.folder, "meta.json").read_text())["id"], "replacement"
            )
            killpg.assert_not_called()
            socket.assert_not_called()

    def test_permission_denial_is_never_a_successful_exit_or_lost_identity(self):
        process, control = self.process()
        with mock.patch("socket.socket") as socket:
            connection = socket.return_value.__enter__.return_value
            stream = connection.makefile.return_value.__enter__.return_value
            stream.readline.side_effect = [
                b'{"error": "permission denied", "errno": 1}\n',
                b'{"error": "permission denied", "errno": 1}\n',
                b'{"live": false}\n',
            ]
            with self.assertRaisesRegex(DriverError, "permission denied"):
                process.wait_exit(0)
            with self.assertRaisesRegex(DriverError, "permission denied"):
                process.kill()
            process.kill()
            self.assertTrue(process.wait_exit(0))
            self.assertEqual(
                connection.connect.call_args_list,
                [mock.call(os.path.join(control.path, "control"))] * 3,
            )


if __name__ == "__main__":
    unittest.main()
