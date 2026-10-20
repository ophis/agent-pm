"""agent-pm-linear issue archive: Linear archives an issue's sub-issues with it, so archive only when the
issue and every unarchived descendant are finished, read over every children page at every level
first; a failed read raises before anything is written.
"""

from agent_pm.orchestrator.trackers.linear import issue_page, issue_read, issue_write
from agent_pm.orchestrator.trackers.linear.linear_tracker import M_ARCHIVE
from agent_pm.orchestrator.trackers.linear.ops import DONE
from agent_pm.orchestrator.trackers.linear.types import ArchiveRefused, LinearError

SHOWN = 20
_NODE = "id identifier state { name type }"


def _children(after=""):
    return (
        f"children(first: {issue_read.PAGE}{after}) {{ nodes {{ {_NODE} }} "
        "pageInfo { hasNextPage endCursor } }"
    )


Q_ARCHIVE = (
    "query($i: String!) { viewer { id name } issue(id: $i) { "
    f"{_NODE} url archivedAt {_children()} }} }}"
)
Q_CHILDREN = f"query($i: String!, $c: String) {{ issue(id: $i) {{ {_children(', after: $c')} }} }}"


def archive(gql, ident):
    """Archive ident once it and every unarchived descendant are finished; ArchiveRefused names the
    unfinished ones.
    """
    node, viewer, descendants = _plan(gql, ident)
    if node["archivedAt"] is not None:
        return issue_write.receipt(node, "archive", "unchanged", viewer)
    if unfinished := _unfinished(node, descendants):
        raise ArchiveRefused(_refusal(ident, unfinished, len(descendants)))
    issue_write.write(gql, "archive", ident, M_ARCHIVE, "issueArchive", i=node["id"])
    return issue_write.receipt(node, "archive", "changed", viewer)


def dry_run(gql, ident):
    """What archive would do, with the descendants it would archive and the unfinished issues."""
    node, _, descendants = _plan(gql, ident)
    archived = node["archivedAt"] is not None
    unfinished = [] if archived else _unfinished(node, descendants)
    return {
        "issue": {"id": node["id"], "identifier": node["identifier"], "url": node["url"]},
        "operation": "archive",
        "dry_run": True,
        "result": "unchanged" if archived else "refused" if unfinished else "changed",
        "descendant_count": len(descendants),
        "descendants": [_item(n) for n in descendants],
        "unfinished_count": len(unfinished),
        "unfinished": [_item(n) for n in unfinished],
        "truncated": [],
    }


def _plan(gql, ident):
    """ident's issue, the viewer and, unless the issue is archived, its unarchived descendants level
    by level (Linear lists no archived sub-issue).
    """
    data = issue_read.lookup(gql, Q_ARCHIVE, ident)
    node, descendants = data["issue"], []
    if node["archivedAt"] is None:
        level, seen = [node], {node["id"]}
        while level:
            below = []
            for parent in level:
                for child in _all_children(gql, ident, parent):
                    if child["id"] not in seen:
                        seen.add(child["id"])
                        below.append(child)
            descendants += below
            level = below
    return node, data["viewer"], descendants


def _all_children(gql, ident, parent):
    first = parent["children"] if "children" in parent else _page(gql, parent, None)
    page, seen = _checked(ident, parent, first), set()
    nodes = list(page["nodes"])
    while page["pageInfo"]["hasNextPage"]:
        after = issue_page.advance(
            f"{parent['identifier']} children", page["pageInfo"]["endCursor"], seen
        )
        page = _checked(ident, parent, _page(gql, parent, after))
        nodes += page["nodes"]
    return nodes


def _page(gql, parent, after):
    return (gql(Q_CHILDREN, i=parent["id"], c=after)["issue"] or {}).get("children")


def _checked(ident, parent, page):
    if issue_read.is_page(page):
        return page
    raise LinearError(
        f"archive {ident}: sub-issues of {parent['identifier']} unreadable "
        "(deleted or archived mid-read?): nothing archived"
    )


def _unfinished(node, descendants):
    return [n for n in (node, *descendants) if n["state"]["type"] not in DONE]


def _item(node):
    return {"id": node["id"], "identifier": node["identifier"], "state": node["state"]["name"]}


def _refusal(ident, unfinished, descendants):
    shown = ", ".join(f"{n['identifier']} ({n['state']['name']})" for n in unfinished[:SHOWN])
    more = f" and {len(unfinished) - SHOWN} more" if len(unfinished) > SHOWN else ""
    return (
        f"archive {ident} refused, nothing archived: {len(unfinished)} unfinished of "
        f"{descendants + 1} (it and its unarchived sub-issues): {shown}{more}; complete or cancel "
        f"each first (agent-pm-linear issue archive {ident} --dry-run lists them)"
    )
