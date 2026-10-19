"""Linear issue nodes, as `agent-pm-linear issue read`, `move`, `comment` and `archive` query them,
and recording fake gqls; the lookup and list entities by edge with Routes, a fake gql answering each
by its root field; IssueCase runs issue.main against a test configuration.
"""

import io
import os
import re

from agent_pm.linear_orchestrator.engine import (
    client,
    issue,
    issue_archive,
    issue_read,
    issue_write,
)
from agent_pm.linear_orchestrator.engine.types import GraphQLErrors
from orchestrator_tests.case import Case
from orchestrator_tests.unit import promote_fixtures as tp
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.run_fixtures import CORE_LOCAL

OTHER_TEAM = "00000000-0000-4000-8000-0000000000f2"


class IssueCase(Case):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.home, "root")
        os.makedirs(self.root)
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as f:
            f.write(f'[linear_orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL)
        self.config = os.path.join(self.root, "config.toml")
        with open(self.config, "w") as f:
            f.write(tp.CONFIG)

    def run_main(self, argv, gql, stdin=b""):
        out, err = io.BytesIO(), io.StringIO()
        code = issue.main(
            argv, gql=gql, config_path=self.config, out=out, err=err, stdin=io.BytesIO(stdin)
        )
        return code, out.getvalue(), err.getvalue()


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


def uid(n):
    return f"00000000-0000-4000-8000-{n:012x}"


def edges(nodes, *, more=False, cursor=None):
    found = [{"cursor": f"cur-{n['id']}", "node": n} for n in nodes]
    return {"edges": found, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def team_ref(n, key, name=None):
    return {"id": uid(n), "name": name or f"team {key}", "key": key}


def state(n, name, kind="unstarted", team_id=TEAM):
    return {"id": uid(n), "name": name, "type": kind, "team": {"id": team_id}}


def label(n, name, team_id=TEAM, *, group=None, is_group=False, retired=None):
    return {
        "id": uid(n),
        "name": name,
        "isGroup": is_group,
        "retiredAt": retired,
        "team": {"id": team_id} if team_id else None,
        "parent": {"id": group} if group else None,
    }


def project(n, name, team_ids=(TEAM,), *, more=False):
    teams = {"nodes": [{"id": t} for t in team_ids], "pageInfo": {"hasNextPage": more}}
    return {"id": uid(n), "name": name, "teams": teams}


def milestone(n, name, project_id):
    return {"id": uid(n), "name": name, "project": {"id": project_id}}


def user(n, name, email=None, *, active=True):
    return {"id": uid(n), "name": name, "email": email or f"u{n}@example.com", "active": active}


class Routes:
    """A fake gql answering a query by its root field (teams, team, issueLabels, …): routes[field]
    is a list of answers taken in order, else one answer for every call; an answer is the field's
    value, a callable of the variables giving it, or an exception it raises. Records every
    (query, variables) call.
    """

    def __init__(self, **routes):
        self.routes = {k: list(v) if isinstance(v, list) else v for k, v in routes.items()}
        self.calls = []

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        field = re.match(r"query[^{]*\{\s*(\w+)", query).group(1)
        answer = self.routes[field]
        answer = answer.pop(0) if isinstance(answer, list) else answer
        answer = answer(variables) if callable(answer) else answer
        if isinstance(answer, BaseException):
            raise answer
        return {field: answer}

    def sent(self, field):
        """The variables of every call to field."""
        return [v for q, v in self.calls if re.match(r"query[^{]*\{\s*" + field + r"\b", q)]


def issue_row(ident, team_id=TEAM, **fields):
    """An issue node as agent-pm-linear issue list queries it."""
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "title": f"title {ident}",
        "url": f"https://linear.app/t/issue/{ident}",
        "state": {"id": uid(3), "name": "Todo"},
        "assignee": None,
        "project": None,
        "parent": None,
        "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "team": {"id": team_id},
        **fields,
    }


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


NEW = {"id": "id-TASK-9", "identifier": "TASK-9", "url": "https://linear.app/t/issue/TASK-9"}


def by_name(*nodes):
    """A Routes answer: the edges of nodes whose name the query's filter names exactly."""

    def find(found):
        if isinstance(found, dict):
            if isinstance(found.get("name"), dict) and "eq" in found["name"]:
                return found["name"]["eq"]
            found = list(found.values())
        if isinstance(found, list):
            return next((n for n in map(find, found) if n is not None), None)
        return None

    return lambda variables: edges([n for n in nodes if n["name"] == find(variables)])


class CreateGql(Routes):
    """Routes for create's reads; issueCreate answers `created` (default: NEW made), each
    issueRelationCreate the next of `relations` (default: success); an exception there is raised.
    Records every (query, variables) call.
    """

    def __init__(self, *, created=None, relations=(), **routes):
        super().__init__(**routes)
        self.created, self.relations = created, list(relations)

    def __call__(self, query, **variables):
        if not query.startswith("mutation"):
            return super().__call__(query, **variables)
        self.calls.append((query, variables))
        field = client.operation(query)
        if field == "issueCreate":
            answer = {"success": True, "issue": NEW} if self.created is None else self.created
        else:
            answer = self.relations.pop(0) if self.relations else {"success": True}
        if isinstance(answer, BaseException):
            raise answer
        return {field: answer}

    @property
    def writes(self):
        return [call for call in self.calls if call[0].startswith("mutation")]


def _issue_ref(variables):
    ident = variables["id"]
    if ident.startswith("NOPE"):
        return GraphQLErrors([{"message": "Entity not found: Issue"}])
    return {"id": f"id-{ident}", "identifier": ident}


def create_gql(**overrides):
    """A CreateGql knowing team TEAM's states Backlog and Todo, labels bug, ops (workspace) and old
    (retired), project Agent PM with milestone M1, user Ann, and every issue but NOPE-*.
    """
    routes = {
        "workflowStates": by_name(state(2, "Backlog", "backlog"), state(3, "Todo")),
        "issueLabels": by_name(
            label(4, "bug"), label(5, "ops", None), label(6, "old", retired="2026-01-01")
        ),
        "team": lambda v: {"projects": by_name(project(7, "Agent PM"))(v)},
        "project": lambda v: {"projectMilestones": by_name(milestone(8, "M1", uid(7)))(v)},
        "users": by_name(user(9, "Ann")),
        "issue": _issue_ref,
        "viewer": VIEWER,
    }
    return CreateGql(**{**routes, **overrides})
