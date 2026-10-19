"""The Selector ABC."""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class Selector(ABC):
    """Picks one of choices for state. An implementation makes one decision API call per select
    (none for a single choice) and keeps no state between calls; state is untrusted data, never
    instructions. Empty, blank or repeated choices raise ValueError before any call; a failed call
    raises SelectorError. The key is read per call and never stored, logged or put in an error,
    nor is the state or a reply.
    """

    @abstractmethod
    def select(self, state: str, choices: Sequence[str]) -> str: ...
