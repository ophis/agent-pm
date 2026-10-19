"""agent-pm-linear issue read: one issue's fields, newest comments and relations as a view (view),
cut to the default limits by compact, or with every page for --full.
"""

import re
from typing import TypeGuard

from agent_pm.linear_orchestrator.engine.client import messages
from agent_pm.linear_orchestrator.engine.types import (
    ISSUE_ID,
    UUID_RE,
    GraphQLErrors,
    IssueError,
    LinearError,
)

COMMENTS, COMMENT_CHARS, DESCRIPTION_CHARS, COLUMN = 5, 300, 600, 10
PAGE = 250
NOT_FOUND = "Entity not found"
_REL = "id identifier title state { name }"
CONNECTIONS = {
    "comments": "id body createdAt parentId quotedText user { id name } onBehalfOf { id name }",
    "labels": "id name",
    "relations": f"type relatedIssue {{ {_REL} }}",
    "inverseRelations": f"type issue {{ {_REL} }}",
    "children": _REL,
}
_FIRST = {
    "comments": COMMENTS,
    "labels": 50,
    "relations": 50,
    "inverseRelations": 50,
    "children": COLUMN,
}
# a connection with more pages cuts these view fields
_CUTS = {
    "comments": "comments",
    "labels": "labels",
    "relations": "blocks",
    "inverseRelations": "blocked_by",
    "children": "children",
}


def _connection(name, first, after=""):
    order = ", orderBy: createdAt" if name == "comments" else ""
    nodes = CONNECTIONS[name]
    page = "pageInfo { hasNextPage endCursor }"
    return f"{name}(first: {first}{after}{order}) {{ nodes {{ {nodes} }} {page} }}"


def _query(first):
    return (
        "query($i: String!) { issue(id: $i) { id identifier title url description "
        "state { id name type } project { id name } projectMilestone { id name } "
        f"assignee {{ id name email }} parent {{ {_REL} }} "
        + " ".join(_connection(name, first.get(name, PAGE)) for name in CONNECTIONS)
        + " } }"
    )


Q_READ, Q_FULL = _query(_FIRST), _query({})
MORE = {
    name: f"query($i: String!, $c: String) {{ issue(id: $i) {{ {_connection(name, PAGE, ', after: $c')} }} }}"
    for name in CONNECTIONS
}


def lookup(gql, query, ident):
    """query's data for ident, an issue identifier or id, as $i; IssueError unless its issue is
    there.
    """
    if not (re.fullmatch(ISSUE_ID, ident) or UUID_RE.fullmatch(ident)):
        raise IssueError(f"{ident!r}: want an issue identifier such as TASK-1 or an issue id")
    try:
        data = gql(query, i=ident)
    except LinearError as error:
        if isinstance(error, GraphQLErrors):
            missing = any(m.startswith(NOT_FOUND) for m in messages(error.errors))
        else:
            missing = NOT_FOUND in str(error)
        if not missing:
            raise
        data = None
    if data is None or data["issue"] is None:
        raise IssueError(f"{ident}: no such issue")
    return data


def view(gql, ident, *, full=False):
    """The issue's view: with full, every page of every connection and nothing truncated."""
    node = lookup(gql, Q_FULL if full else Q_READ, ident)["issue"]
    nodes, more = {}, []
    for name in CONNECTIONS:
        page = node[name]
        nodes[name] = list(page["nodes"])
        while full and page["pageInfo"]["hasNextPage"]:
            after = page["pageInfo"]["endCursor"]
            page = _later(gql, ident, node["id"], name, after)
            nodes[name] += page["nodes"]
        if page["pageInfo"]["hasNextPage"]:
            more.append(_CUTS[name])
    return _shape(node, nodes, more)


def _later(gql, ident, issue_id, name, after):
    page = (gql(MORE[name], i=issue_id, c=after)["issue"] or {}).get(name)
    if is_page(page):
        return page
    raise LinearError(f"{ident}: {name} page missing: deleted or archived mid-read?")


def is_page(page) -> TypeGuard[dict]:
    """Whether page is a connection page: nodes and pageInfo."""
    return (
        isinstance(page, dict)
        and isinstance(page.get("nodes"), list)
        and isinstance(page.get("pageInfo"), dict)
    )


def _shape(node, nodes, truncated):
    comments = sorted(nodes["comments"], key=lambda c: c["createdAt"], reverse=True)
    return {
        "id": node["id"],
        "identifier": node["identifier"],
        "title": node["title"],
        "url": node["url"],
        "state": node["state"],
        "labels": [{"id": n["id"], "name": n["name"]} for n in nodes["labels"]],
        "project": node["project"],
        "milestone": node["projectMilestone"],
        "assignee": node["assignee"],
        "description": node["description"],
        "comments": [_comment(c) for c in comments],
        "blocks": [_rel(r["relatedIssue"]) for r in nodes["relations"] if r["type"] == "blocks"],
        "blocked_by": [
            _rel(r["issue"]) for r in nodes["inverseRelations"] if r["type"] == "blocks"
        ],
        "parent": _rel(node["parent"]) if node["parent"] else None,
        "children": [_rel(n) for n in nodes["children"]],
        "truncated": truncated,
        "continue": None,
    }


def _comment(node):
    return {
        "id": node["id"],
        "author": node["user"],
        "created_at": node["createdAt"],
        "body": node["body"],
        "parent_id": node["parentId"],
        "quoted_text": node["quotedText"],
        "on_behalf_of": node["onBehalfOf"],
    }


def _rel(node):
    return {
        "id": node["id"],
        "identifier": node["identifier"],
        "title": node["title"],
        "state": node["state"]["name"],
    }


def compact(full):
    """full cut to the default limits; truncated names each cut field, or one with more pages."""
    out, pages, cuts = dict(full), set(full["truncated"]), []
    if full["description"] is not None and len(full["description"]) > DESCRIPTION_CHARS:
        out["description"] = full["description"][:DESCRIPTION_CHARS]
        cuts.append("description")
    if "comments" in pages or len(full["comments"]) > COMMENTS:
        cuts.append("comments")
    out["comments"] = [dict(c) for c in full["comments"][:COMMENTS]]
    for i, c in enumerate(out["comments"]):
        if len(c["body"]) > COMMENT_CHARS:
            c["body"] = c["body"][:COMMENT_CHARS]
            cuts.append(f"comments.{i}.body")
    if "labels" in pages:
        cuts.append("labels")
    for column in ("blocks", "blocked_by", "children"):
        out[column] = full[column][:COLUMN]
        if column in pages or len(full[column]) > COLUMN:
            cuts.append(column)
    out["truncated"] = cuts
    return out
