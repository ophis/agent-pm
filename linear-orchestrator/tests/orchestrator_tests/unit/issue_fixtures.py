"""Linear issue nodes, as `agent-pm-linear issue read`, `move`, `comment` and `archive` query them, and
recording fake gqls.
"""

from agent_pm.linear_orchestrator.engine import client, issue_archive, issue_read, issue_write


def rel(ident, *, title=None, state="Todo"):
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "title": title or f"title {ident}",
        "state": {"name": state},
    }


def conn(nodes, *, more=False, cursor=None):
    return {"nodes": list(nodes), "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def comment(n, **fields):
    return {
        "id": f"c{n}",
        "body": f"comment {n}",
        "createdAt": f"2026-10-{n:02d}T00:00:00.000Z",
        "parentId": None,
        "quotedText": None,
        "user": {"id": "u-me", "name": "Me"},
        "onBehalfOf": None,
        **fields,
    }


def issue_node(ident="TASK-2", **fields):
    node = {
        "id": f"id-{ident}",
        "identifier": ident,
        "title": f"title {ident}",
        "url": f"https://linear.app/t/issue/{ident}",
        "description": None,
        "state": {"id": "s-todo", "name": "Todo", "type": "unstarted"},
        "project": None,
        "projectMilestone": None,
        "assignee": None,
        "parent": None,
        **{name: conn([]) for name in issue_read.CONNECTIONS},
    }
    node.update(fields)
    return node


class Gql:
    """Answers the read query with `node`, each later page of a connection from `pages[name]` (in
    order; an exception there is raised, None or a dict without nodes answers as the whole issue), and records every (query, variables) call; an exception
    in `fail` is raised instead.
    """

    def __init__(self, node, pages=None, fail=None):
        self.node, self.fail, self.calls = node, fail, []
        self.pages = {name: list(found) for name, found in (pages or {}).items()}

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if self.fail is not None:
            raise self.fail
        for name, more in issue_read.MORE.items():
            if query == more:
                page = self.pages[name].pop(0)
                if isinstance(page, BaseException):
                    raise page
                return (
                    {"issue": page}
                    if page is None or "nodes" not in page
                    else {"issue": {name: page}}
                )
        return {"issue": self.node}


VIEWER = {"id": "u-me", "name": "Me"}
STATES = (("s-todo", "Todo", "unstarted"), ("s-doing", "In Progress", "started"))


def team(key="TASK", states=STATES, *, more=False, cursor=None):
    nodes = [{"id": i, "name": n, "type": t} for i, n, t in states]
    return {"id": f"t-{key}", "key": key, "states": conn(nodes, more=more, cursor=cursor)}


def write_issue(ident="TASK-2", *, state="s-todo", team_node=None):
    """An issue as `agent-pm-linear issue move` and `comment` read it."""
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "url": f"https://linear.app/t/issue/{ident}",
        "state": {"id": state},
        "team": team_node or team(ident.split("-")[0]),
    }


class WriteGql:
    """Answers a write's read with `issue` (None: no such issue) and VIEWER, each later page of
    states from `pages` (in order), and each mutation with `result`, raised when an exception;
    an exception in `fail` is raised by the read instead. Records every (query, variables) call.
    """

    DEFAULTS = {
        "issueUpdate": {"success": True},
        "commentCreate": {"success": True, "comment": {"id": "c-new"}},
    }

    def __init__(self, issue, *, pages=(), result=None, fail=None, viewer=VIEWER):
        self.issue, self.result, self.fail, self.viewer = issue, result, fail, viewer
        self.pages, self.calls = list(pages), []

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if query.startswith("mutation"):
            if isinstance(self.result, BaseException):
                raise self.result
            field = client.operation(query)
            return {field: self.DEFAULTS[field] if self.result is None else self.result}
        if query == issue_write.Q_STATES:
            return {"team": {"states": self.pages.pop(0)}}
        if self.fail is not None:
            raise self.fail
        return {"viewer": self.viewer, "issue": self.issue}

    @property
    def writes(self):
        return [call for call in self.calls if call[0].startswith("mutation")]


def tree(ident="TASK-1", kind="completed", *children, archived=None):
    """An issue as `agent-pm-linear issue archive` reads it, its unarchived sub-issues children."""
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "url": f"https://linear.app/t/issue/{ident}",
        "archivedAt": archived,
        "state": {"name": kind.title(), "type": kind},
        "kids": list(children),
    }


class ArchiveGql:
    """Answers the archive read with `root` (None: no such issue) and VIEWER, each children
    connection in pages of issue_read.PAGE (cursor: the next offset), and the mutation with
    `result`, raised when an exception. `broken` maps (issue id, offset) to an exception that page
    raises, or None for a page missing. Records every (query, variables) call.
    """

    def __init__(self, root, *, broken=None, result=None, viewer=VIEWER):
        self.root, self.broken, self.result, self.viewer = root, dict(broken or {}), result, viewer
        self.nodes, self.calls, stack = {}, [], [root] if root else []
        while stack:
            node = stack.pop()
            self.nodes[node["id"]] = node
            stack += node["kids"]

    def _page(self, node, offset):
        if (node["id"], offset) in self.broken:
            if (error := self.broken[node["id"], offset]) is not None:
                raise error
            return None
        kids = node["kids"][offset : offset + issue_read.PAGE]
        more = offset + issue_read.PAGE < len(node["kids"])
        nodes = [{k: kid[k] for k in ("id", "identifier", "state")} for kid in kids]
        return conn(nodes, more=more, cursor=str(offset + issue_read.PAGE) if more else None)

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        if query.startswith("mutation"):
            if isinstance(self.result, BaseException):
                raise self.result
            return {"issueArchive": {"success": True} if self.result is None else self.result}
        if query == issue_archive.Q_CHILDREN:
            page = self._page(self.nodes[variables["i"]], int(variables["c"] or 0))
            return {"issue": None if page is None else {"children": page}}
        if self.root is None:
            return {"viewer": self.viewer, "issue": None}
        node = {k: v for k, v in self.root.items() if k != "kids"}
        return {"viewer": self.viewer, "issue": node | {"children": self._page(self.root, 0)}}

    @property
    def writes(self):
        return [call for call in self.calls if call[0].startswith("mutation")]
