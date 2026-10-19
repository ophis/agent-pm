"""FakeSelector, a Selector that calls no decision API: it picks the first of picks among the
question's options, else the first option; error, when given, is raised instead; calls records each
(state, question).
"""

from collections.abc import Sequence

from agent_pm.selector import Question, Selector, SelectorError, State


class FakeSelector(Selector):
    def __init__(self, picks: Sequence[str] = (), *, error: SelectorError | None = None):
        self.picks = tuple(picks)
        self.error = error
        self.calls: list[tuple[State, Question]] = []

    def select(self, state: State, question: Question) -> str:
        self.calls.append((state, question))
        if self.error is not None:
            raise self.error
        return next((p for p in self.picks if p in question.options), next(iter(question.options)))
