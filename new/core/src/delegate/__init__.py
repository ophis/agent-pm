"""The Delegate: runs one role as an agent session (run), its prompt and access intent from the Composer, its session
from the Driver (driver.api), and reads the agent run's reports in its replies: progress mark lines and one outcome
block, whose deliverable_file it reads in. Every check runs first (plan); a refusal is ConfigError, nothing started or
written. An invalid outcome gets one fix resume of the same session; a usage limit stops the session and ends the run
(Result.limit). Replies, marks and stderr lines are printed to stderr, printable; a saving destination's deliverable is
saved to `out`; the records input, progress, idle, limit, invalid, missing, result and end go to <workdir>/run.jsonl
beside the Driver's. on_event gets Begun once, a Progress per mark line and Finished only with a valid outcome; its
exception stops the session and propagates. It imports only composer, driver.api, utils and the stdlib
(tests/guards.py layer_imports). Modules: reply (a reply's mark lines and outcome block), outcome (an outcome block read
and validated; save), plan (a run's checks, cwd, Spec and prompt), runner (a run to its Result, the headless policy),
cli (the command line `python3 new/core/src/delegate/cli.py`, home of the exit codes)."""
from delegate.outcome import STATUSES, InvalidOutcome, Outcome
from delegate.plan import ConfigError, Plan, plan
from delegate.runner import IDLE_TIMEOUT, Begun, Finished, Limit, Progress, Result, run

__all__ = ["ConfigError", "Plan", "plan", "run", "Result", "Limit", "Outcome", "InvalidOutcome", "Begun", "Progress",
           "Finished", "IDLE_TIMEOUT", "STATUSES"]
