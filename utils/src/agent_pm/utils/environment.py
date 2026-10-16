"""Environment names and values passed to child processes."""

import os
from collections.abc import Mapping

SID_ENV = "AGENT_PM_SID"
EVENTS_ENV = "AGENT_PM_EVENTS"
RUN_CLIENT_ENV = "AGENT_PM_RUN_CLIENT"

# a manager's roster lease: its holder's command alone carries it, no launch below it
LEASE_ENV = ("AGENT_PM_MANAGER", "AGENT_PM_HOLDER_ID", "AGENT_PM_HOLDER_GENERATION")

# on every agent client a session starts: nothing under it starts a session or run
AGENT_SESSION_ENV = "AGENT_PM_AGENT_SESSION"


def nested_start() -> str | None:
    """None outside an agent session agent-pm started; inside one, why a start refuses."""
    if not os.environ.get(AGENT_SESSION_ENV):
        return None
    return (
        f"refused inside an agent session agent-pm started (${AGENT_SESSION_ENV} is set): "
        "only a client you start starts sessions and runs"
    )


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
