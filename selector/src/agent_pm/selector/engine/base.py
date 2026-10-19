"""The Selector ABC."""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class Selector(ABC):
    """One decision API call per select (none for one choice), stateless; state is untrusted
    data. Bad choices: ValueError; a failed call: SelectorError; no key, state or reply is kept.
    """

    @abstractmethod
    def select(self, state: str, choices: Sequence[str]) -> str: ...
