"""Delegate Spec fields, defaults and immutability."""

import dataclasses
import unittest

from agent_pm import delegate

from delegate.tests.case import Case

SPEC = {
    "input": None,
    "workdir": None,
    "task": None,
    "mode": "headless",
    "runtime": None,
    "sid": None,
    "resume": False,
    "name": None,
    "events": None,
    "out": None,
    "repo": None,
    "cwd": None,
    "team": None,
}


class SpecTest(Case):
    def test_a_frozen_spec_its_fields_and_defaults(self):
        found = {
            f.name: None if f.default is dataclasses.MISSING else f.default
            for f in dataclasses.fields(delegate.Spec)
        }
        self.assertEqual(found, SPEC)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            delegate.Spec("x", "/w").sid = "s"


if __name__ == "__main__":
    unittest.main()
