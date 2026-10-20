"""The packaged default workflow (workflow.toml): each orchestrated role's Stage, read on first use."""

import dataclasses
import tomllib
from collections.abc import Iterator, Mapping
from importlib import resources
from typing import get_args

from agent_pm.orchestrator.engine.flow.types import Stage

REL = "orchestrator/src/agent_pm/orchestrator/engine/flow/workflow.toml"
CHOICES = {k: get_args(Stage.__annotations__[k]) for k in ("input", "first_failure")}
# every field but input has a default, of the field's type
FIELDS = {f.name: type(f.default) for f in dataclasses.fields(Stage) if f.name != "input"}


class Workflow(Mapping[str, Stage]):
    """Role → Stage, read by the first access unless load() read it."""

    _tasks: Mapping[str, Stage] | None = None

    def load(self) -> Mapping[str, Stage]:
        """Reread; a ValueError (message starting with REL) keeps the last read."""
        try:
            text = (
                resources.files("agent_pm.orchestrator.engine.flow")
                .joinpath("workflow.toml")
                .read_text(encoding="utf-8")
            )
            data = tomllib.loads(text)
        except (OSError, ValueError) as e:  # TOMLDecodeError is a ValueError
            raise ValueError(f"{REL}: {e}") from None
        self._tasks = _parse(data)
        return self._tasks

    def __getitem__(self, role: str) -> Stage:
        return self._read()[role]

    def __iter__(self) -> Iterator[str]:
        return iter(self._read())

    def __len__(self) -> int:
        return len(self._read())

    def _read(self) -> Mapping[str, Stage]:
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
    if extra := sorted(set(table) - {"input", *FIELDS}):
        raise ValueError(f"{at}: unknown keys: {', '.join(extra)}")
    for key, value in table.items():
        want = FIELDS.get(key)
        if want is not None and type(value) is not want:
            raise ValueError(f"{at}.{key}: want a {want.__name__}: {value!r}")
    for key, choices in CHOICES.items():
        if (key in table or key == "input") and table.get(key) not in choices:
            raise ValueError(f"{at}.{key}: want one of {', '.join(choices)}: {table.get(key)!r}")
    return Stage(**table)


DEFAULT = Workflow()
