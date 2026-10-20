"""The flow Tracker on Linear, from a gql callable, the team (ops.Team) and {user id: role}: its
states mapped to IssueState, its reads and writes in Linear's query text."""

from agent_pm.linear_orchestrator.engine.ops import (
    DONE,
    HISTORY,
    call,
    humans,
    last_move,
    role_ids,
    task_group,
    team,
)
from agent_pm.linear_orchestrator.engine.types import LinearError, State
from agent_pm.linear_orchestrator.flow.tracker import Tracker
from agent_pm.linear_orchestrator.flow.types import (
    UNREADABLE,
    Finished,
    IssueState,
    Snapshot,
    SubIssue,
)
from agent_pm.utils import one_line, parse_time

# Last wins where two keys share an id, so todo and in_progress skip it as before.
STATES = {
    State.IN_REVIEW: IssueState.REVIEW,
    State.HANDOFF: IssueState.APPROVED,
    State.TODO: IssueState.READY,
    State.IN_PROGRESS: IssueState.RUNNING,
}
LISTED = {IssueState.READY: State.TODO, IssueState.RUNNING: State.IN_PROGRESS}

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
RELATIONS = "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"
Q_RELATIONS = "query($i: String!) { issue(id: $i) { " + RELATIONS + " } }"
Q_RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } } }"
)
Q_HISTORY = "query($i: String!, $after: String) { issue(id: $i) { " + HISTORY + " } }"


def q_issues(fields=""):
    return (
        (
            "query($f: IssueFilter) { issues(filter: $f, first: 100) { nodes { id identifier url priority createdAt "
            "updatedAt project { id name } assignee { id email } "
        )
        + fields
        + " } } }"
    )


def blockers(issue):
    nodes = [n for n in issue["inverseRelations"]["nodes"] if n["type"] == "blocks"]
    return [
        n["issue"]["identifier"] if n.get("issue") else UNREADABLE
        for n in nodes
        if not n.get("issue") or n["issue"]["state"]["type"] not in DONE
    ]


def finished_at(issue, finished):
    """The latest of issue's move into finished and its canceledAt and completedAt (the history
    read can lack a move into Duplicate that canceledAt holds); None: unknown."""
    times = [parse_time(issue[k]) for k in ("canceledAt", "completedAt") if issue[k]]
    if moved := last_move(issue["history"]["nodes"], finished):
        times.append(moved)
    return max(times, default=None)


def _quiet(*_, **__):
    pass


class LinearTracker(Tracker):
    """humans: the human members' user ids, which connect reads; say: the caller's logger."""

    def __init__(self, gql, team, roles, humans=frozenset(), say=_quiet):
        self.gql, self.team, self.roles, self.humans, self.say = gql, team, roles, humans, say
        self.states = {team.states[k]: v for k, v in STATES.items()}
        self.hist = {}

    @classmethod
    def connect(cls, gql, cfg, *, say):
        """Checks the team and task label group, then reads the human and role accounts."""
        found = team(gql, cfg)
        task_group(gql, cfg)
        people = frozenset(humans(gql, cfg))
        return cls(gql, found, role_ids(gql, cfg.roles), people, say)

    def role(self, issue):
        return self.roles[issue["assignee"]["id"]]

    def assigned(self, state):
        return self._listed(state)

    def _listed(self, state, fields=""):
        """state's issues with a project, assigned to a role account; the first 100."""
        if state not in LISTED:
            raise ValueError(f"no list of {state} issues")
        flt = {
            "team": {"id": {"eq": self.team.id}},
            "project": {"null": False},
            "assignee": {"id": {"in": list(self.roles)}},
            "state": {"id": {"eq": self.team.states[LISTED[state]]}},
        }
        return self.gql(q_issues(fields), f=flt)["issues"]["nodes"]

    def ready(self):
        """Relations come with the list; when that read fails, a logged todo-error, the plain list
        and one read per issue, an unreadable one blocked by UNREADABLE."""
        try:
            issues = self._listed(IssueState.READY, RELATIONS)
        except LinearError as e:
            self.say("todo-error", error=one_line(e))
            issues = self._listed(IssueState.READY)
            for i in issues:
                try:
                    i.update(self.gql(Q_RELATIONS, i=i["id"])["issue"])
                except LinearError:
                    i["inverseRelations"] = {"nodes": [{"type": "blocks", "issue": None}]}
        return [(i, tuple(blockers(i))) for i in issues]

    def moved_at(self, key, state, *, by_people=False):
        """History read once per key; by_people: moves by the human members only."""
        if key not in self.hist:
            self.hist[key] = self.gql(Q_HISTORY, i=key)["issue"]["history"]["nodes"]
        moved = {self.team.states[LISTED[state]]}
        return last_move(self.hist[key], moved, self.humans if by_people else None)

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
