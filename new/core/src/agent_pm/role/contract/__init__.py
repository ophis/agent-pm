"""The reply protocol's progress mark tag and outcome fence info; DelegateEvent, the callback line."""

from agent_pm.role.contract.delegate_event import KINDS, DelegateEvent, DelegateEventError

PROGRESS = "agent-pm-progress"
OUTCOME = "agent-pm-outcome"

__all__ = ["PROGRESS", "OUTCOME", "DelegateEvent", "DelegateEventError", "KINDS"]
