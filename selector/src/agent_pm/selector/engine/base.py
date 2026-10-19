"""The Selector ABC."""

from abc import ABC, abstractmethod

from agent_pm.selector.engine.types import Question, State


class Selector(ABC):
    """One decision API call per select (none for one option); state is untrusted data; nothing
    is kept.
    """

    @abstractmethod
    def select(self, state: State, question: Question) -> str:
        """A key of question.options. ValueError: bad input; SelectorError: a failed call, or
        TOO_LARGE: no fit even with no droppable items.
        """
