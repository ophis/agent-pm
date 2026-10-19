"""SelectorContract, the Selector contract: a subclass also inherits Case and defines make() (a
Selector). Each case uses only the Selector ABC and that hook, for both question kinds.
"""

from agent_pm.selector import Answer, Kind, Question, Selector, State

STATE = State(
    {"title": "Release notes", "description": "Write the release notes for a small docs change."},
    (
        "comments",
        (
            {"author": "A", "created_at": "2026-10-19T09:00:00Z", "body": "Keep them short."},
            {"author": "B", "created_at": "2026-10-19T10:00:00Z", "body": "Link the docs PR."},
        ),
    ),
)
QUESTION = Question(
    "Choose the effort.",
    {
        "low": "the least reasoning",
        "medium": "more reasoning than low",
        "high": "the most reasoning",
    },
)

SCORE = Question("Score the effort.", dict(QUESTION.options), Kind.SCORE)


class SelectorContract:
    def make(self) -> Selector:
        raise NotImplementedError

    def test_the_answer_picks_one_of_the_options(self):
        for question in (QUESTION, SCORE):
            with self.subTest(kind=question.kind):
                answer = self.make().select(STATE, question)
                self.assertIsInstance(answer, Answer)
                self.assertIn(answer.pick, question.options)
                confidence = answer.confidence
                self.assertTrue(confidence is None or 0 <= confidence <= 1, confidence)

    def test_a_single_option_is_picked_without_a_confidence(self):
        only = Question(QUESTION.instructions, {"high": "the most reasoning"})
        self.assertEqual(self.make().select(STATE, only), Answer("high", None))

    def test_the_same_input_gives_the_same_answer(self):
        selector = self.make()
        for question in (QUESTION, SCORE):
            with self.subTest(kind=question.kind):
                self.assertEqual(selector.select(STATE, question), selector.select(STATE, question))
