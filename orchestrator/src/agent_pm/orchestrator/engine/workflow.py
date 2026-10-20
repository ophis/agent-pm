"""The packaged default workflow (workflow.toml): each orchestrated role's Task, read on first use."""

import dataclasses
import tomllib
from collections.abc import Iterator, Mapping
from importlib import resources
from typing import get_args

from agent_pm.orchestrator.engine.types import Task

REL = "orchestrator/src/agent_pm/orchestrator/engine/workflow.toml"
CHOICES = {k: get_args(Task.__annotations__[k]) for k in ("kind", "failed_new")}
# every field but kind has a default, of the field's type
FIELDS = {f.name: type(f.default) for f in dataclasses.fields(Task) if f.name != "kind"}


class Workflow(Mapping[str, Task]):
    """Role → Task, read by the first access unless load() read it."""

    _tasks: Mapping[str, Task] | None = None

    def load(self) -> Mapping[str, Task]:
        """Reread; a ValueError (message starting with REL) keeps the last read."""
        try:
            text = (
                resources.files("agent_pm.orchestrator.engine")
                .joinpath("workflow.toml")
                .read_text(encoding="utf-8")
            )
            data = tomllib.loads(text)
        except (OSError, ValueError) as e:  # TOMLDecodeError is a ValueError
            raise ValueError(f"{REL}: {e}") from None
        self._tasks = _parse(data)
        return self._tasks

    def __getitem__(self, role: str) -> Task:
        return self._read()[role]

    def __iter__(self) -> Iterator[str]:
        return iter(self._read())

    def __len__(self) -> int:
        return len(self._read())

    def _read(self) -> Mapping[str, Task]:
        return self._tasks if self._tasks is not None else self.load()


def _parse(data):
    roles = data.get("roles")
    if set(data) != {"roles"} or not isinstance(roles, dict):
        raise ValueError(f"{REL}: want only a [roles] table of [roles.<role>] tables")
    return {role: _task(role, table) for role, table in roles.items()}


def _task(role, table):
    at = f"{REL}: roles.{role}"
    if not isinstance(table, dict):
        raise ValueError(f"{at}: want a table")
    if extra := sorted(set(table) - {"kind", *FIELDS}):
        raise ValueError(f"{at}: unknown keys: {', '.join(extra)}")
    for key, value in table.items():
        want = FIELDS.get(key)
        if want is not None and type(value) is not want:
            raise ValueError(f"{at}.{key}: want a {want.__name__}: {value!r}")
    for key, choices in CHOICES.items():
        if (key in table or key == "kind") and table.get(key) not in choices:
            raise ValueError(f"{at}.{key}: want one of {', '.join(choices)}: {table.get(key)!r}")
    return Task(**table)


DEFAULT = Workflow()
