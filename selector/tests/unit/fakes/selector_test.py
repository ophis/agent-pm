"""selector/tests/fakes/selector.py: FakeSelector passes SelectorContract and follows its script."""

import unittest

from agent_pm.selector import Answer, Failure, Kind, Question, SelectorError, State

from selector.tests.contracts.selector import SelectorContract
from selector.tests.fakes.selector import FakeSelector
from utils.tests.case import Case

STATE = State({"title": "s"})
AB = Question("Pick.", {"a": "desc a", "b": "desc b"})
CD = Question("Pick again.", {"c": "desc c", "d": "desc d"})
LEVELS = Question("Score.", {"low": "least", "mid": "more", "top": "most"}, Kind.SCORE)


class FakeSelectorContract(SelectorContract, Case):
    def make(self):
        return FakeSelector()


class Script(Case):
    def test_the_first_scripted_pick_among_the_options_wins(self):
        fake = FakeSelector(["z", "d", "b", "mid"])
        self.assertEqual(fake.select(STATE, AB), Answer("b", 1.0))
        self.assertEqual(fake.select(STATE, CD), Answer("d", 1.0))
        self.assertEqual(fake.select(STATE, LEVELS), Answer("mid", 1.0))
        self.assertEqual(FakeSelector(("b", "a")).select(STATE, AB).pick, "b")

    def test_no_scripted_pick_among_the_options_gives_the_first_option(self):
        self.assertEqual(FakeSelector(["z"]).select(STATE, AB).pick, "a")
        self.assertEqual(FakeSelector().select(STATE, CD).pick, "c")
        self.assertEqual(FakeSelector().select(STATE, LEVELS).pick, "low")

    def test_the_confidence_is_scripted_and_none_for_a_single_option(self):
        for confidence in (0.2, 0.0, None):
            with self.subTest(confidence=confidence):
                fake = FakeSelector(["b"], confidence=confidence)
                self.assertEqual(fake.select(STATE, AB), Answer("b", confidence))
                self.assertEqual(fake.select(STATE, LEVELS), Answer("low", confidence))
        only = Question("Pick.", {"a": "desc a"})
        self.assertEqual(FakeSelector(confidence=0.2).select(STATE, only), Answer("a", None))

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
