"""selector/tests/fakes/selector.py: FakeSelector passes SelectorContract and follows its script."""

import unittest

from agent_pm.selector import Failure, Question, SelectorError, State

from selector.tests.contracts.selector import SelectorContract
from selector.tests.fakes.selector import FakeSelector
from utils.tests.case import Case

STATE = State({"title": "s"})
AB = Question("Pick.", {"a": "desc a", "b": "desc b"})
CD = Question("Pick again.", {"c": "desc c", "d": "desc d"})


class FakeSelectorContract(SelectorContract, Case):
    def make(self):
        return FakeSelector()


class Script(Case):
    def test_the_first_scripted_pick_among_the_options_wins(self):
        fake = FakeSelector(["z", "d", "b"])
        self.assertEqual(fake.select(STATE, AB), "b")
        self.assertEqual(fake.select(STATE, CD), "d")
        self.assertEqual(FakeSelector(("b", "a")).select(STATE, AB), "b")

    def test_no_scripted_pick_among_the_options_gives_the_first_option(self):
        self.assertEqual(FakeSelector(["z"]).select(STATE, AB), "a")
        self.assertEqual(FakeSelector().select(STATE, CD), "c")

    def test_a_scripted_error_is_raised(self):
        error = SelectorError(Failure.TIMEOUT)
        with self.assertRaises(SelectorError) as caught:
            FakeSelector(error=error).select(STATE, AB)
        self.assertIs(caught.exception, error)

    def test_calls_records_each_state_and_question(self):
        fake = FakeSelector()
        other = State({"title": "t"}, ("comments", ("c",)))
        fake.select(STATE, AB)
        fake.select(other, CD)
        self.assertEqual(fake.calls, [(STATE, AB), (other, CD)])


if __name__ == "__main__":
    unittest.main()
