"""launch(spec) (it may keep state for events) builds a Launch whose env, never its resume, carries
Access.env and Spec.env (the Client is their only owner); events() turns the client's output into
SessionEvents, Stderr and ClientBound (JsonHeadless: parse(record) per JSON line).
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator

from agent_pm.session.engine.types import (
    ClientBound,
    ClientHome,
    ClientRuntime,
    Launch,
    SessionEvent,
    Spec,
    Stderr,
)
from agent_pm.utils import json_records


class Headless(ABC):
    _runtime: ClientRuntime | None = None

    def _set_runtime(self, runtime: ClientRuntime | None) -> None:
        self._runtime = runtime

    def _home(self) -> ClientHome | None:
        return None

    def commit(self) -> None:
        return None

    @abstractmethod
    def launch(self, spec: Spec) -> Launch: ...

    @abstractmethod
    def events(self, lines: Iterable[str]) -> Iterator[SessionEvent | Stderr | ClientBound]: ...


class JsonHeadless(Headless):
    @abstractmethod
    def parse(self, record: dict) -> Iterable[SessionEvent | Stderr | ClientBound]: ...

    def events(self, lines: Iterable[str]) -> Iterator[SessionEvent | Stderr | ClientBound]:
        for record in json_records(lines):
            yield from self.parse(record)
