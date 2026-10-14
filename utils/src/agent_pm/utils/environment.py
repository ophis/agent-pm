"""Environment names and values passed to child processes."""

from collections.abc import Mapping


def environment_ok(value: object) -> bool:
    """Accept exec names: nonempty, no '=' or NUL; string values without NUL.

    Names need not be shell identifiers. Render them as env operands in shell commands.
    """
    return isinstance(value, Mapping) and all(
        isinstance(name, str)
        and bool(name)
        and "=" not in name
        and "\0" not in name
        and isinstance(item, str)
        and "\0" not in item
        for name, item in value.items()
    )
