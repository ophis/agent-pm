"""FakeSelector, a Selector that calls no decision API: it picks the first of picks among the
question's options, else the first option, either kind, answering with confidence (None for a
single option); error, when given, is raised instead; calls records each (state, question).
"""

from collections.abc import Sequence

from agent_pm.selector import Answer, Question, Selector, SelectorError, State


class FakeSelector(Selector):
    def __init__(
        self,
        picks: Sequence[str] = (),
        *,
        confidence: float | None = 1.0,
        error: SelectorError | None = None,
    ):
        self.picks = tuple(picks)
        self.confidence = confidence
        self.error = error
        self.calls: list[tuple[State, Question]] = []

    def select(self, state: State, question: Question) -> Answer:
        self.calls.append((state, question))
        if self.error is not None:
            raise self.error
        pick = next((p for p in self.picks if p in question.options), next(iter(question.options)))
        return Answer(pick, None if len(question.options) == 1 else self.confidence)
