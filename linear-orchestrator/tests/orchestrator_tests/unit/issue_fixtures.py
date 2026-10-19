"""Linear issue nodes, as `agent-pm-linear issue read` queries them, and a recording fake gql; the
lookup and list entities by edge with Routes, a fake gql answering each by its root field; IssueCase
runs issue.main against a test configuration.
"""

import io
import os
import re

from agent_pm.linear_orchestrator.engine import issue, issue_read
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

    def run_main(self, argv, gql):
        out, err = io.BytesIO(), io.StringIO()
        code = issue.main(argv, gql=gql, config_path=self.config, out=out, err=err)
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


def team(n, key, name=None):
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
        field = re.search(r"\)\s*\{\s*(\w+)", query).group(1)
        answer = self.routes[field]
        answer = answer.pop(0) if isinstance(answer, list) else answer
        answer = answer(variables) if callable(answer) else answer
        if isinstance(answer, BaseException):
            raise answer
        return {field: answer}

    def sent(self, field):
        """The variables of every call to field."""
        return [v for q, v in self.calls if re.search(r"\)\s*\{\s*" + field + r"\b", q)]
