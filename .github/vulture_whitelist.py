"""Names Vulture cannot see used, one entry each with its reason above it. An interface waiting for
its caller names the ticket that calls it; that ticket deletes the entry.
"""

from agent_pm.delegate.composer.teams import TeamKind
from agent_pm.delegate.event.types import AutopilotStatus
from agent_pm.linear_orchestrator.engine import (
    issues,
    promote,
    prune,
    router,
    run_comments,
    types,
    writeback,
)
from agent_pm.linear_orchestrator.engine.issue_refs import milestone
from agent_pm.linear_orchestrator.engine.types import State
from agent_pm.session.clients.claude import ClaudeClient
from agent_pm.session.clients.codex import CodexClient
from agent_pm.session.clients.opencode import OpenCodeClient
from agent_pm.session.engine.base import Session
from agent_pm.session.engine.types import Effort, NamedKey, SessionRow, Tier
from agent_pm.session.viewer.types import Split
from agent_pm.utils import LayerCommand, OutcomeStatus

# Enum members reached only by iteration or by value:
# teams.py reads each TeamKind dir.
_ = TeamKind.ROLES, TeamKind.TEMPLATES
# outcome.py parses OutcomeStatus(status).
_ = OutcomeStatus.NEEDS_INPUT, OutcomeStatus.FAILED
# outcome.py parses AutopilotStatus(status).
_ = AutopilotStatus.CAPPED, AutopilotStatus.STOPPED
# engine/config.py and ops.py iterate State for the [states] keys.
_ = State.TODO, State.IN_PROGRESS, State.IN_REVIEW, State.CANCELED
# prune re-exports its queries, now linear_tracker's, only for tests; 第 2 阶段删除.
_ = prune.M_ARCHIVE, prune.Q_CHILDREN, prune.Q_FINISHED, prune.Q_ISSUE
# promote re-exports its queries, now linear_tracker's, only for tests; 第 2 阶段删除.
_ = promote.M_CREATE, promote.M_RELATE, promote.Q_CHILD, promote.Q_DETAIL, promote.Q_HANDOFF
# router re-exports its queries and blockers, now linear_tracker's, only for tests; 第 2 阶段删除.
_ = router.RELATIONS, router.Q_RELATIONS, router.Q_RECHECK, router.Q_HISTORY, router.q_issues
_ = router.blockers
# run_comments re-exports its query, now linear_tracker's, and keeps write(gql, ...) only for tests;
# 第 2 阶段删除.
_ = run_comments.M_UPDATE, run_comments.write
# run_comments re-exports these, now linear_tracker's, only for tests; 第 2 阶段删除.
_ = run_comments.URL, run_comments.is_record
# writeback re-exports its queries, now linear_tracker's, only for tests; 第 2 阶段删除.
_ = (
    writeback.M_ATTACH,
    writeback.M_TITLE,
    writeback.M_UNARCHIVE,
    writeback.Q_ATTACHED,
    writeback.Q_ID,
)
# issues re-exports the run issue read, now linear_tracker's, and the Handoff parser, now
# flow.handoff's, only for tests; 第 2 阶段删除.
_ = issues.Q_ISSUE, issues.read_issue, issues.parse_handoff
# engine.types re-exports Linked, now flow.types', only for tests; 第 2 阶段删除.
_ = types.Linked
# Record checks ctl[0] against LayerCommand's values.
_ = LayerCommand.LINEAR, LayerCommand.SESSION
# Effort(value) parses a run's effort; clients/common.py lists the values.
_ = Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX
# Spec checks its tier, and clients/common.py a tiers table's keys, against Tier's values.
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

# session/config.toml loads each Client by its `implementation` path.
_ = ClaudeClient, CodexClient, OpenCodeClient
# TASK-403's harness sets its PYTHONPATH from Session.class_roots (D16).
_ = Session.class_roots
# TASK-506 routes sid-checked CLI closes through Session.retire; delete this entry then.
_ = Session.retire
# Only tests read it, but the session interface is frozen (tests/guard_checks/exports.py
# session_interface_snapshot; TASK-396).
_ = SessionRow.last_lines

# TASK-653's issue create resolves --milestone with it.
_ = milestone
