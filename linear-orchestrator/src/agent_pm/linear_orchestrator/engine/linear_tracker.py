"""The flow Tracker on Linear, from a gql callable: its states mapped to IssueState, its reads and
writes in Linear's query text."""

from typing import TypeVar

from agent_pm.linear_orchestrator.engine.ops import DONE, HISTORY, call, last_move
from agent_pm.linear_orchestrator.engine.types import State
from agent_pm.linear_orchestrator.flow.tracker import Tracker
from agent_pm.linear_orchestrator.flow.types import Finished, IssueState, Snapshot, SubIssue
from agent_pm.utils import parse_time

T = TypeVar("T")

# Last wins where two keys share an id, so todo and in_progress skip it as before.
STATES = {
    State.IN_REVIEW: IssueState.REVIEW,
    State.HANDOFF: IssueState.APPROVED,
    State.TODO: IssueState.READY,
    State.IN_PROGRESS: IssueState.RUNNING,
}

STAMPS = "canceledAt completedAt"
Q_ISSUE = (
    "query($i: String!, $after: String) { issue(id: $i) { state { id } "
    + STAMPS
    + " "
    + HISTORY
    + " } }"
)
CHILDREN = "children(first: 250) { nodes { id identifier state { type } } }"
Q_CHILDREN = "query($i: String!) { issue(id: $i) { " + CHILDREN + " } }"
Q_FINISHED = (
    """query($t: ID, $d: [ID!], $s: [ID!], $a: [ID!], $c: String, $after: String) {
  issues(filter: { team: { id: { eq: $t } }, or: [{ state: { id: { in: $d } }, assignee: { id: { in: $a } } },
  { state: { id: { in: $s } } }] }, first: 50, after: $c) {
  nodes { id identifier """
    + STAMPS
    + " "
    + CHILDREN
    + " "
    + HISTORY
    + """ }
  pageInfo { hasNextPage endCursor } } }"""
)
M_ARCHIVE = "mutation($i: String!) { issueArchive(id: $i) { success } }"
M_TITLE = (
    "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }"
)
M_ATTACH = "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }"
M_UNARCHIVE = "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
Q_ATTACHED = "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }"
Q_ID = "query($i: String!) { issue(id: $i) { id team { id } } }"
M_UPDATE = (
    "mutation($c: String!, $b: String!) { commentUpdate(id: $c, input: { body: $b }) { success } }"
)
ATTACHED = ("already been linked", "Duplicate attachment", "Unable to create issue attachment")


def finished_at(issue, finished):
    """The latest of issue's move into finished and its canceledAt and completedAt (the history
    read can lack a move into Duplicate that canceledAt holds); None: unknown."""
    times = [parse_time(issue[k]) for k in ("canceledAt", "completedAt") if issue[k]]
    if moved := last_move(issue["history"]["nodes"], finished):
        times.append(moved)
    return max(times, default=None)


class LinearTracker(Tracker):
    """states: {State key: state id}; team_id; finished: ops.Team.finished; roles: {user id: role}.
    A method needing one not given raises TypeError."""

    def __init__(self, gql, *, states=None, team_id=None, finished=None, roles=None):
        self.gql, self.ids, self.team_id, self.roles = gql, states, team_id, roles
        self.ended = finished
        self.states = {states[k]: v for k, v in STATES.items()} if states else {}

    def read(self, ident):
        ids, ended = _need("states", self.ids), _need("finished", self.ended)
        issue = self.gql(Q_ISSUE, i=ident)["issue"]
        if not issue:
            return None
        state = issue["state"]["id"]
        found = self.states.get(state)
        if found not in (IssueState.READY, IssueState.RUNNING) and state in ended:
            done = state == ids[State.DONE]
            return Snapshot(
                IssueState.DONE if done else IssueState.CANCELED, finished_at(issue, ended)
            )
        return Snapshot(found, last_move(issue["history"]["nodes"], {state}))

    def finished(self, owners):
        states, team = _need("states", self.ids), _need("team_id", self.team_id)
        roles, ended = _need("roles", self.roles), _need("finished", self.ended)
        ids = [uid for uid, role in roles.items() if role in owners]
        done = sorted(ended & {states[State.DONE]})
        nodes, cursor = [], None
        while True:
            page = self.gql(
                Q_FINISHED,
                t=team,
                d=done,
                s=sorted(ended.difference(done)),
                a=ids,
                c=cursor,
            )["issues"]
            nodes += page["nodes"]
            end = page["pageInfo"]["endCursor"]
            if not page["pageInfo"]["hasNextPage"] or not end or end == cursor:
                break
            cursor = end
        return [
            Finished(
                n["id"],
                n["identifier"],
                finished_at(n, ended),
                _sub_issues(n["children"]["nodes"]),
            )
            for n in nodes
        ]

    def sub_issues(self, key):
        return _sub_issues(self.gql(Q_CHILDREN, i=key)["issue"]["children"]["nodes"])

    def archive(self, key):
        call(self.gql, M_ARCHIVE, "issueArchive", i=key)


def _need(name: str, value: T | None) -> T:
    if value is None:
        raise TypeError(f"LinearTracker needs {name}")
    return value


def _sub_issues(nodes):
    return tuple(SubIssue(n["id"], n["identifier"], n["state"]["type"] in DONE) for n in nodes)
