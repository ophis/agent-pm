"""SelectorContract, the Selector contract: a subclass also inherits Case and defines make() (a
Selector). Each case uses only the Selector ABC and that hook.
"""

from agent_pm.selector import Selector

STATE = "Write the release notes for a small docs change."
CHOICES = ("low", "medium", "high")


class SelectorContract:
    def make(self) -> Selector:
        raise NotImplementedError

    def test_the_pick_is_one_of_the_choices(self):
        self.assertIn(self.make().select(STATE, CHOICES), CHOICES)

    def test_a_single_choice_is_picked(self):
        self.assertEqual(self.make().select(STATE, ("high",)), "high")

    def test_the_same_input_gives_the_same_pick(self):
        selector = self.make()
        self.assertEqual(selector.select(STATE, CHOICES), selector.select(STATE, CHOICES))

    def test_empty_blank_or_repeated_choices_are_refused(self):
        for choices in ((), ("low", " "), ("low", "low")):
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                self.make().select(STATE, choices)
