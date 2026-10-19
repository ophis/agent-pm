"""agent-pm-linear issue move and comment: resolve the target within the issue's own team, read a
body verbatim, write once and answer with a receipt; a write Linear never answered is WriteUnknown,
never retried.
"""

from agent_pm.linear_orchestrator.engine import issue_read, ops
from agent_pm.linear_orchestrator.engine.types import IssueError, Unavailable, WriteUnknown


def _states_page(after=""):
    return f"states(first: 250{after}) {{ nodes {{ id name type }} pageInfo {{ hasNextPage endCursor }} }}"


Q_MOVE = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url state { id } "
    f"team {{ id key {_states_page()} }} }} }}"
)
Q_COMMENT = "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url } }"
M_COMMENT = (
    "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) "
    "{ success comment { id } } }"
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


def read_body(path, stdin):
    """The UTF-8 text of path, or of stdin (bytes) for -, verbatim; IssueError when it cannot be read
    or holds only whitespace.
    """
    try:
        if path == "-":
            raw = stdin.read()
        else:
            with open(path, "rb") as f:
                raw = f.read()
        body = raw.decode("utf-8")
    except OSError as error:
        raise IssueError(f"--body-file {path}: {error.strerror or error}") from None
    except UnicodeDecodeError:
        raise IssueError(f"--body-file {path}: not UTF-8") from None
    if not body.strip():
        raise IssueError(f"--body-file {path}: empty body")
    return body


def comment(gql, ident, body):
    """Post body as a new top-level comment on ident."""
    data = issue_read.lookup(gql, Q_COMMENT, ident)
    node = data["issue"]
    out = write(gql, "comment", ident, M_COMMENT, "commentCreate", i=node["id"], b=body)
    comment_id = (out.get("comment") or {}).get("id")
    return receipt(node, "comment", "changed", data["viewer"], comment_id=comment_id)


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
