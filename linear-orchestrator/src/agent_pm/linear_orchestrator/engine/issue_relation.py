"""agent-pm-linear issue relation: add or remove one blocking edge, found from the given issue's own
side, so `A --blocks B` and `B --blocked-by A` name the same edge; any other relation stays.
"""

from agent_pm.linear_orchestrator.engine import issue_read, issue_refs, issue_write
from agent_pm.linear_orchestrator.engine.types import IssueError, LinearError

# direction: the issue's connection holding the edge, and the field naming its other end
SIDES = {"blocks": ("relations", "relatedIssue"), "blocked_by": ("inverseRelations", "issue")}


def _side(direction, after=""):
    name, end = SIDES[direction]
    return (
        f"{name}(first: {issue_read.PAGE}{after}) {{ nodes {{ id type {end} {{ id }} }} "
        "pageInfo { hasNextPage endCursor } }"
    )


Q_READ = {
    d: f"query($i: String!) {{ viewer {{ id name }} issue(id: $i) {{ id identifier url {_side(d)} }} }}"
    for d in SIDES
}
Q_MORE = {
    d: f"query($i: String!, $c: String) {{ issue(id: $i) {{ {_side(d, ', after: $c')} }} }}"
    for d in SIDES
}
M_ADD = (
    "mutation($i: String!, $r: String!) { issueRelationCreate(input: "
    "{ issueId: $i, relatedIssueId: $r, type: blocks }) { success } }"
)
M_REMOVE = "mutation($i: String!) { issueRelationDelete(id: $i) { success } }"


def relation(gql, op, ident, direction, other):
    """add or remove the edge ident blocks other (direction blocks) or other blocks ident
    (blocked_by).
    """
    option = "--" + direction.replace("_", "-")
    data = issue_read.lookup(gql, Q_READ[direction], ident)
    node = data["issue"]
    target = issue_refs.issue(gql, other, option=option)
    if target["id"] == node["id"]:
        raise IssueError(f"{option} {other}: {ident} cannot block itself")
    end = SIDES[direction][1]
    edge = [
        r
        for r in _all(gql, ident, node, direction)
        if r["type"] == "blocks" and (r[end] or {}).get("id") == target["id"]
    ]
    operation = f"relation {op}"
    if (op == "add") == bool(edge):
        return issue_write.receipt(node, operation, "unchanged", data["viewer"])
    if op == "add":
        blocker, blocked = (node, target) if direction == "blocks" else (target, node)
        variables = {"i": blocker["id"], "r": blocked["id"]}
        issue_write.write(gql, operation, ident, M_ADD, "issueRelationCreate", **variables)
    else:
        for record in edge:
            issue_write.write(
                gql, operation, ident, M_REMOVE, "issueRelationDelete", i=record["id"]
            )
    return issue_write.receipt(node, operation, "changed", data["viewer"])


def _all(gql, ident, node, direction):
    name = SIDES[direction][0]
    page = node[name]
    records = list(page["nodes"])
    while page["pageInfo"]["hasNextPage"]:
        after = page["pageInfo"]["endCursor"]
        page = (gql(Q_MORE[direction], i=node["id"], c=after)["issue"] or {}).get(name)
        if not issue_read.is_page(page):
            raise LinearError(f"{ident}: {name} page missing: deleted mid-read?")
        records += page["nodes"]
    return records
