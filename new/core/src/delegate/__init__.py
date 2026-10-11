"""The Delegate: runs one role as an agent session, its prompt and access intent from the Composer, its session from
the Driver (driver.api), and reads the agent run's reports in its replies: progress mark lines and one outcome block.
It imports only composer, driver.api, utils and the stdlib (tests/guards.py layer_imports). Modules: reply (a reply's
mark lines and outcome block), outcome (an outcome block read and validated, its deliverable_file read in; save), plan
(a run's checks, cwd, Spec and prompt before anything starts)."""
from delegate.outcome import STATUSES, InvalidOutcome, Outcome
from delegate.plan import ConfigError, Plan, plan

__all__ = ["ConfigError", "Plan", "plan", "Outcome", "InvalidOutcome", "STATUSES"]
