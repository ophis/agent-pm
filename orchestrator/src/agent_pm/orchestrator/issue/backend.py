"""The tracker operations the issue command runs. Each returns plain data and raises only the errors
the frame maps to an exit code, translating its tracker's own failures into them.
"""

from argparse import Namespace
from typing import Any, Protocol

from agent_pm.orchestrator.engine.flow.types import ArchivePlan

Row = tuple[dict[str, Any], list[str], str]


class Backend(Protocol):
    def view(self, ident: str, *, full: bool) -> dict[str, Any]:
        """The uncut view; truncated: the fields with more pages."""
        ...

    def page(
        self, args: Namespace, first: int, cursor: str | None
    ) -> tuple[list[Row], bool, str | None]:
        """list or lookup's page after cursor: (rows, has_next_page, the last row's cursor)."""
        ...

    def walk(self, args: Namespace) -> list[Row]: ...

    def create(self, args: Namespace, body: str) -> dict[str, Any]:
        """The receipt; RelationsFailed when a blocking relation failed."""
        ...

    def edit(self, args: Namespace, body: str | None) -> dict[str, Any]: ...

    def move(self, ident: str, state: str) -> dict[str, Any]: ...

    def comment(self, ident: str, body: str) -> dict[str, Any]:
        """The receipt, with comment_id."""
        ...

    def relation(self, op: str, ident: str, direction: str, other: str) -> dict[str, Any]: ...

    def archive_plan(self, ident: str) -> ArchivePlan: ...

    def archive(self, ident: str) -> dict[str, Any]:
        """The receipt; ArchiveRefused(plan) when an issue is unfinished."""
        ...
