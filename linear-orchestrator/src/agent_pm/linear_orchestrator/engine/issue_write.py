"""agent-pm-linear issue move: resolve the target within the issue's own team, write once and
answer with a receipt; a write Linear never answered is WriteUnknown, never retried.
"""

from agent_pm.linear_orchestrator.engine import issue_read, ops
from agent_pm.linear_orchestrator.engine.types import IssueError, Unavailable, WriteUnknown


def _states_page(after=""):
    return f"states(first: 250{after}) {{ nodes {{ id name type }} pageInfo {{ hasNextPage endCursor }} }}"


Q_MOVE = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url state { id } "
    f"team {{ id key {_states_page()} }} }} }}"
)
Q_STATES = f"query($t: String!, $c: String) {{ team(id: $t) {{ {_states_page(', after: $c')} }} }}"


def move(gql, ident, value):
    """Move ident to the state of its team whose id or exact name is value."""
    data = issue_read.lookup(gql, Q_MOVE, ident)
    node = data["issue"]
    state = _state(_states(gql, node["team"]), value, node["team"]["key"])
    if node["state"]["id"] == state["id"]:
        return receipt(node, "move", "unchanged", data["viewer"])
    write(gql, "move", ident, ops.M_STATE, "issueUpdate", i=node["id"], s=state["id"])
    return receipt(node, "move", "changed", data["viewer"])


def _states(gql, team):
    page = team["states"]
    states = list(page["nodes"])
    while page["pageInfo"]["hasNextPage"]:
        page = gql(Q_STATES, t=team["id"], c=page["pageInfo"]["endCursor"])["team"]["states"]
        states += page["nodes"]
    return states


def _state(states, value, key):
    found = [s for s in states if value in (s["id"], s["name"])]
    if len(found) == 1:
        return found[0]
    if found:
        why = "is ambiguous"
    elif any(s["type"] == value for s in states):
        why = "is a state type, not a state name"
    else:
        why = "is not a state"
    names = ", ".join(f"{s['name']} ({s['id']})" for s in found or states)
    raise IssueError(f"state {value!r} {why} of team {key}: {names}")


def write(gql, op, ident, query, field, **variables):
    """The mutation's result; WriteUnknown when Linear never answered it."""
    try:
        return ops.call(gql, query, field, **variables)
    except Unavailable as error:
        raise WriteUnknown(
            f"{op} {ident}: result unknown ({error}): "
            f"check with agent-pm-linear issue read {ident} before retrying"
        ) from None


def receipt(node, op, result, actor, **extra):
    return {
        "issue": {"id": node["id"], "identifier": node["identifier"], "url": node["url"]},
        "operation": op,
        "result": result,
        "actor": {"id": actor["id"], "name": actor["name"]},
        **extra,
    }
