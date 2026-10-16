"""Names Vulture cannot see used, one entry each with its reason above it. An interface waiting for
its caller names the ticket that calls it; that ticket deletes the entry.
"""

from agent_pm.delegate.composer.teams import TeamKind
from agent_pm.delegate.event.types import AutopilotStatus, OutcomeStatus
from agent_pm.linear_orchestrator.engine.types import State
from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.engine.base import Session
from agent_pm.session.engine.types import Effort, NamedKey, SessionRow, Tier
from agent_pm.session.hosts.tmux.types import Split
from agent_pm.utils.record import LayerCommand

# Enum members reached only by iteration or by value:
# teams.py reads each TeamKind dir.
_ = TeamKind.ROLES, TeamKind.TEMPLATES
# outcome.py parses OutcomeStatus(status).
_ = OutcomeStatus.NEEDS_INPUT, OutcomeStatus.FAILED
# outcome.py parses AutopilotStatus(status); TASK-473's ready gate reads CONVERGED.
_ = AutopilotStatus.CONVERGED, AutopilotStatus.CAPPED, AutopilotStatus.STOPPED
# engine/config.py and ops.py iterate State for the [states] keys.
_ = State.TODO, State.IN_PROGRESS, State.IN_REVIEW, State.CANCELED
# Record checks ctl[0] against LayerCommand's values.
_ = LayerCommand.DELEGATE, LayerCommand.LINEAR, LayerCommand.SESSION
# Effort(value) parses a run's effort; adapters/common.py lists the values.
_ = Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX
# Spec checks its tier, and adapters/common.py a tiers table's keys, against Tier's values.
_ = Tier.ONE, Tier.TWO, Tier.THREE, Tier.FOUR
# Key matches a `send --key` name by value.
_ = (
    NamedKey.ENTER,
    NamedKey.ESCAPE,
    NamedKey.TAB,
    NamedKey.BTAB,
    NamedKey.BSPACE,
    NamedKey.UP,
    NamedKey.DOWN,
    NamedKey.LEFT,
    NamedKey.RIGHT,
    NamedKey.HOME,
    NamedKey.F1,
    NamedKey.F2,
    NamedKey.F3,
    NamedKey.F4,
    NamedKey.F5,
    NamedKey.F6,
    NamedKey.F7,
    NamedKey.F8,
    NamedKey.F9,
    NamedKey.F10,
    NamedKey.F11,
    NamedKey.F12,
)
# The tmux Host checks a split by value.
_ = Split.RIGHT, Split.BELOW

# session/config.toml loads each Adapter by its `implementation` path.
_ = ClaudeAdapter, CodexAdapter
# TASK-403's harness sets its PYTHONPATH from Session.class_roots (D16).
_ = Session.class_roots
# Only tests read it, but the session interface is frozen (tests/guard_checks/exports.py
# session_interface_snapshot; TASK-396).
_ = SessionRow.last_lines
