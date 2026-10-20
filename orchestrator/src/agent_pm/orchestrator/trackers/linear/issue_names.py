"""agent-pm-orchestrator issue's abstract names (states, roles, tasks, clients, priorities) as the
configured Linear ids; IssueError for a name the configuration lacks, before anything is written.
"""

from agent_pm.orchestrator.engine.flow.types import IssueState, Priority
from agent_pm.orchestrator.engine.types import IssueError
from agent_pm.orchestrator.trackers.linear import ops
from agent_pm.orchestrator.trackers.linear.linear_tracker import MOVES

PRIORITIES = {
    Priority.NONE: 0,
    Priority.URGENT: 1,
    Priority.HIGH: 2,
    Priority.MEDIUM: 3,
    Priority.LOW: 4,
}
Q_VIEWER = "query { viewer { id } }"
_KEYS = {state: key for key, state in MOVES.items()}


def state_id(cfg, state: IssueState) -> str:
    """The id of the [linear.states] key state is."""
    return cfg.states[_KEYS[state]]


def assignee(gql, cfg, spec) -> str | None:
    """The user --role spec names: a role's account, me (the harness_key account) or unassigned."""
    if spec in cfg.roles:
        account = cfg.roles[spec].account
        if (found := ops.user_id(gql, account)) is None:
            raise IssueError(f"--role {spec}: account {account} not found in Linear")
        return found
    if spec == "me":
        return gql(Q_VIEWER)["viewer"]["id"]
    if spec == "unassigned":
        return None
    raise IssueError(f"--role {spec}: not a role: {', '.join([*cfg.roles, 'me', 'unassigned'])}")


def task_label(cfg, name) -> str:
    return _label("--task", name, cfg.task_labels, "[orchestrator.linear.task_labels]")


def client_label(cfg, name) -> str:
    if not cfg.agent_label_group:
        raise IssueError(
            f"--client {name}: client labels are not configured "
            "([orchestrator.linear] agent_label_group)"
        )
    return _label("--client", name, cfg.agent_labels, "[orchestrator.linear.agent_labels]")


def _label(option, name, labels, table):
    if name in labels:
        return labels[name]
    if not labels:
        raise IssueError(f"{option} {name}: none configured ({table})")
    raise IssueError(f"{option} {name}: not a name of {table}: {', '.join(labels)}")


def in_team(cfg, node, option):
    """IssueError unless node, an issue read with team { id key }, is of the configured team."""
    if node["team"]["id"] != cfg.team:
        raise IssueError(
            f"{node['identifier']} is in team {node['team']['key']}: {option} takes only issues "
            f"of the configured team (linear.team {cfg.team})"
        )
