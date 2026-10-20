"""Linear connections read by edge for agent-pm-linear issue list and lookup. A source is (query,
path, variables, shape): query takes $first and $after beside variables, its connection sits at the
dotted path of the data, and shape maps a node to (item, cut fields) or None to skip it.
"""

import functools

from agent_pm.orchestrator.trackers.linear.types import LinearError

PAGE = 250
PAGE_INFO = "pageInfo { hasNextPage endCursor }"


def connection(name, args, fields):
    """GraphQL selection of connection name(args) by edge: each edge's cursor and node fields."""
    paging = "first: $first, after: $after"
    args = f"{args}, {paging}" if args else paging
    return f"{name}({args}) {{ edges {{ cursor node {{ {fields} }} }} {PAGE_INFO} }}"


def fetch(gql, source, first, after=None):
    """One page: ([(item, cuts, cursor), …] of the nodes shape keeps, has_next_page, endCursor)."""
    query, path, variables, shape = source
    data = gql(query, first=first, after=after, **variables)
    page = functools.reduce(
        lambda d, k: d.get(k) if isinstance(d, dict) else None, path.split("."), data
    )
    if not (
        isinstance(page, dict)
        and isinstance(page.get("edges"), list)
        and isinstance(page.get("pageInfo"), dict)
    ):
        raise LinearError(f"{path}: no such connection in the response")
    rows = []
    for edge in page["edges"]:
        if (found := shape(edge["node"])) is not None:
            rows.append((*found, edge["cursor"]))
    return rows, page["pageInfo"]["hasNextPage"], page["pageInfo"]["endCursor"]


def walk(gql, source):
    """Every page's rows, as fetch gives them."""
    rows, after, seen = [], None, set()
    while True:
        page, more, end = fetch(gql, source, PAGE, after)
        rows += page
        if not more:
            return rows
        after = advance(source[1], end, seen)


def advance(where, end, seen):
    """end, the cursor of the page after one claiming more, added to seen; LinearError when it is
    empty or already read, which would read the same page forever.
    """
    if not end or end in seen:
        raise LinearError(f"{where}: hasNextPage without a new endCursor ({end!r})")
    seen.add(end)
    return end
