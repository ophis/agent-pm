"""The flow Tracker on Linear, from a gql callable, the team (ops.Team) and {user id: role}: its
states mapped to IssueState, its reads and writes in Linear's query text."""

from agent_pm.linear_orchestrator.engine.ops import DONE, HISTORY, call, last_move
from agent_pm.linear_orchestrator.engine.types import State
from agent_pm.linear_orchestrator.flow.tracker import Tracker
from agent_pm.linear_orchestrator.flow.types import Finished, IssueState, Snapshot, SubIssue
from agent_pm.utils import parse_time

STATES = {
    State.TODO: IssueState.READY,
    State.IN_PROGRESS: IssueState.RUNNING,
    State.IN_REVIEW: IssueState.REVIEW,
    State.HANDOFF: IssueState.APPROVED,
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


def finished_at(issue, finished):
    """The latest of issue's move into finished and its canceledAt and completedAt (the history
    read can lack a move into Duplicate that canceledAt holds); None: unknown."""
    times = [parse_time(issue[k]) for k in ("canceledAt", "completedAt") if issue[k]]
    if moved := last_move(issue["history"]["nodes"], finished):
        times.append(moved)
    return max(times, default=None)


class LinearTracker(Tracker):
    def __init__(self, gql, team, roles):
        self.gql, self.team, self.roles = gql, team, roles
        self.states = {team.states[k]: v for k, v in STATES.items()}

    def read(self, ident):
        issue = self.gql(Q_ISSUE, i=ident)["issue"]
        if not issue:
            return None
        state = issue["state"]["id"]
        found = self.states.get(state)
        if found not in (IssueState.READY, IssueState.RUNNING) and state in self.team.finished:
            done = state == self.team.states[State.DONE]
            return Snapshot(
                IssueState.DONE if done else IssueState.CANCELED,
                finished_at(issue, self.team.finished),
            )
        return Snapshot(found, last_move(issue["history"]["nodes"], {state}))

    def finished(self, owners):
        ids = [uid for uid, role in self.roles.items() if role in owners]
        done = sorted(self.team.finished & {self.team.states[State.DONE]})
        nodes, cursor = [], None
        while True:
            page = self.gql(
                Q_FINISHED,
                t=self.team.id,
                d=done,
                s=sorted(self.team.finished.difference(done)),
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
                finished_at(n, self.team.finished),
                _sub_issues(n["children"]["nodes"]),
            )
            for n in nodes
        ]

    def sub_issues(self, key):
        return _sub_issues(self.gql(Q_CHILDREN, i=key)["issue"]["children"]["nodes"])

    def archive(self, key):
        call(self.gql, M_ARCHIVE, "issueArchive", i=key)


def _sub_issues(nodes):
    return tuple(SubIssue(n["id"], n["identifier"], n["state"]["type"] in DONE) for n in nodes)
