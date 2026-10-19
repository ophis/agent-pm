"""selector/tests/fakes/selector.py: FakeSelector passes SelectorContract and follows its script."""

import unittest

from agent_pm.selector import Failure, SelectorError

from selector.tests.contracts.selector import SelectorContract
from selector.tests.fakes.selector import FakeSelector
from utils.tests.case import Case


class FakeSelectorContract(SelectorContract, Case):
    def make(self):
        return FakeSelector()


class Script(Case):
    def test_a_scripted_pick_wins_for_its_state(self):
        fake = FakeSelector({"s": "b"})
        self.assertEqual(fake.select("s", ("a", "b")), "b")
        self.assertEqual(fake.select("other", ("a", "b")), "a")

    def test_a_scripted_pick_outside_the_choices_is_invalid(self):
        with self.assertRaises(SelectorError) as caught:
            FakeSelector({"s": "z"}).select("s", ("a", "b"))
        self.assertEqual(caught.exception.reason, Failure.INVALID)

    def test_a_scripted_error_is_raised(self):
        error = SelectorError(Failure.TIMEOUT)
        with self.assertRaises(SelectorError) as caught:
            FakeSelector(error=error).select("s", ("a",))
        self.assertIs(caught.exception, error)

    def test_calls_records_each_state_and_choices(self):
        fake = FakeSelector()
        fake.select("s", ["a", "b"])
        fake.select("t", ("c",))
        self.assertEqual(fake.calls, [("s", ("a", "b")), ("t", ("c",))])


if __name__ == "__main__":
    unittest.main()
