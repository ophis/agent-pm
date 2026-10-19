"""FakeSelector, a Selector that calls no decision API: it picks picks[state] when given, else the
first choice; error, when given, is raised instead; calls records each (state, choices).
"""

from collections.abc import Mapping, Sequence

from agent_pm.selector import Failure, Selector, SelectorError


class FakeSelector(Selector):
    def __init__(
        self, picks: Mapping[str, str] | None = None, *, error: SelectorError | None = None
    ):
        self.picks = dict(picks or {})
        self.error = error
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def select(self, state: str, choices: Sequence[str]) -> str:
        choices = tuple(choices)
        self.calls.append((state, choices))
        if not choices or any(not c.strip() for c in choices) or len(set(choices)) < len(choices):
            raise ValueError("choices: want one or more distinct, non-blank choices")
        if self.error is not None:
            raise self.error
        pick = self.picks.get(state, choices[0])
        if pick not in choices:
            raise SelectorError(Failure.INVALID)
        return pick
