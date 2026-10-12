"""The reply protocol's progress mark tag and outcome fence info; Event, the callback line."""

from agent_pm.role.contract.event import KINDS, Event, EventError

PROGRESS = "agent-pm-progress"
OUTCOME = "agent-pm-outcome"

__all__ = ["PROGRESS", "OUTCOME", "Event", "EventError", "KINDS"]
