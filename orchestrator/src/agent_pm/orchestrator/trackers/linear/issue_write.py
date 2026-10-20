"""agent-pm-orchestrator issue move and comment: move an issue of the configured team to an abstract
state's configured id, read a body verbatim, write once and answer with a receipt; a write Linear
never answered is WriteUnknown, never retried.
"""

import sys

from agent_pm.orchestrator.engine.types import IssueError, Unavailable, WriteUnknown
from agent_pm.orchestrator.trackers.linear import issue_names, issue_read, ops

Q_MOVE = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url state { id } "
    "team { id key } } }"
)
Q_COMMENT = "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url } }"
M_COMMENT = (
    "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) "
    "{ success comment { id } } }"
)


def move(gql, cfg, ident, state):
    """Move ident, an issue of the configured team, to the abstract state's configured id."""
    data = issue_read.lookup(gql, Q_MOVE, ident)
    node = data["issue"]
    issue_names.in_team(cfg, node, "move")
    target = issue_names.state_id(cfg, state)
    if node["state"]["id"] == target:
        return receipt(node, "move", "unchanged", data["viewer"])
    write(gql, "move", ident, ops.M_STATE, "issueUpdate", i=node["id"], s=target)
    return receipt(node, "move", "changed", data["viewer"])


def read_body(path, stdin=None):
    """The UTF-8 text of path, or for - of stdin (bytes; None: sys.stdin's), verbatim but for a
    leading byte order mark; IssueError when it cannot be read or holds only whitespace and byte
    order marks.
    """
    try:
        if path != "-":
            with open(path, "rb") as f:
                raw = f.read()
        elif (stream := getattr(sys.stdin, "buffer", None) if stdin is None else stdin) is None:
            raise IssueError("--body-file -: no stdin to read")
        else:
            raw = stream.read()
        body = raw.decode("utf-8-sig")
    except OSError as error:
        raise IssueError(f"--body-file {path}: {error.strerror or error}") from None
    except UnicodeDecodeError:
        raise IssueError(f"--body-file {path}: not UTF-8") from None
    if not body.replace("\ufeff", "").strip():
        raise IssueError(f"--body-file {path}: empty body")
    return body


def comment(gql, ident, body):
    """Post body as a new top-level comment on ident."""
    data = issue_read.lookup(gql, Q_COMMENT, ident)
    node = data["issue"]
    out = write(gql, "comment", ident, M_COMMENT, "commentCreate", i=node["id"], b=body)
    comment_id = (out.get("comment") or {}).get("id")
    return receipt(node, "comment", "changed", data["viewer"], comment_id=comment_id)


def write(gql, op, ident, query, field, **variables):
    """The mutation's result; WriteUnknown when Linear never answered it."""
    try:
        return ops.call(gql, query, field, **variables)
    except Unavailable as error:
        raise WriteUnknown(
            f"{op} {ident}: result unknown ({error}): "
            f"check with agent-pm-orchestrator issue read {ident} before retrying"
        ) from None


def receipt(node, op, result, actor, **extra):
    return {
        "issue": {"id": node["id"], "identifier": node["identifier"], "url": node["url"]},
        "operation": op,
        "result": result,
        "actor": {"id": actor["id"], "name": actor["name"]},
        **extra,
    }
