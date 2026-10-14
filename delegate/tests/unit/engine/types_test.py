"""Delegate Spec fields, defaults and immutability."""

import dataclasses
import unittest

from agent_pm import delegate

from delegate.tests.case import Case
from delegate.tests.plans import Base

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


class ResultTest(Case):
    def test_instruction_is_a_path_and_defaults_to_none(self):
        result = delegate.Result(0, None, instruction="/work/prompt.md")
        self.assertEqual(
            dataclasses.asdict(result),
            {
                "returncode": 0,
                "outcome": None,
                "error": "",
                "limit": None,
                "instruction": "/work/prompt.md",
            },
        )
        self.assertIsNone(delegate.Result(0, None).instruction)


class PlanTest(Base):
    def test_a_resolved_plan_has_a_session_id(self):
        planned, _ = self.planned()
        self.assertEqual(planned.sid, planned.spec.sid)

    def test_a_plan_without_a_session_id_is_invalid(self):
        planned, _ = self.planned()
        missing = dataclasses.replace(planned, spec=dataclasses.replace(planned.spec, sid=None))
        with self.assertRaisesRegex(delegate.ConfigError, "plan has no session id"):
            _ = missing.sid


if __name__ == "__main__":
    unittest.main()
