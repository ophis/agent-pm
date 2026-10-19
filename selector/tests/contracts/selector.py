"""SelectorContract, the Selector contract: a subclass also inherits Case and defines make() (a
Selector). Each case uses only the Selector ABC and that hook.
"""

from agent_pm.selector import Question, Selector, State

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


class SelectorContract:
    def make(self) -> Selector:
        raise NotImplementedError

    def test_the_pick_is_one_of_the_options(self):
        self.assertIn(self.make().select(STATE, QUESTION), QUESTION.options)

    def test_a_single_option_is_picked(self):
        only = Question(QUESTION.instructions, {"high": "the most reasoning"})
        self.assertEqual(self.make().select(STATE, only), "high")

    def test_the_same_input_gives_the_same_pick(self):
        selector = self.make()
        self.assertEqual(selector.select(STATE, QUESTION), selector.select(STATE, QUESTION))
