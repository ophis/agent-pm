"""session/tests/fakes/host.py: FakeHost's refusals, before any process starts (the rest: its
integration test).
"""

import os
import sys
import unittest

from session.tests.case import Case
from session.tests.contracts.host import SLEEP, program
from session.tests.fakes.host import FakeHost


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


if __name__ == "__main__":
    unittest.main()
