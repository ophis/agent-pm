"""The flow Tracker on Linear, from a gql callable, the team (ops.Team), {user id: role} and the
human_members emails (comments' other, subscribed on notify): its states mapped to IssueState, its
reads and writes in Linear's query text."""

from agent_pm.linear_orchestrator.engine import ops, run_comments
from agent_pm.linear_orchestrator.engine.ops import DONE, HISTORY, call, client_id, last_move
from agent_pm.linear_orchestrator.engine.types import State
from agent_pm.linear_orchestrator.flow.tracker import Tracker
from agent_pm.linear_orchestrator.flow.types import (
    Approved,
    Comment,
    Detail,
    Finished,
    IssueState,
    Move,
    Ref,
    Snapshot,
    SubIssue,
)
from agent_pm.utils import parse_time

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
Q_HANDOFF = """query($t: ID, $s: ID, $a: [ID!]) { issues(filter: { team: { id: { eq: $t } }, state: { id: { eq: $s } },
  project: { null: false }, assignee: { id: { in: $a } } }, first: 100) {
  nodes { id identifier url title priority createdAt project { id name } assignee { id } attachments { nodes { title url } } } } }"""
Q_DETAIL = (
    """query($i: String!, $after: String) { issue(id: $i) { state { id }
  """
    + HISTORY
    + """
  comments(first: 250) { nodes { body createdAt user { email name isMe } } }
  relations(first: 250) { nodes { relatedIssue { id } } }
  inverseRelations(first: 250) { nodes { issue { id } } } } }"""
)
Q_CHILD = """query($c: ID!) { issues(filter: { id: { eq: $c } }, includeArchived: true) { nodes { id identifier } } }"""
M_CREATE = "mutation($in: IssueCreateInput!) { issueCreate(input: $in) { success issue { id identifier } } }"
M_RELATE = (
    "mutation($in: IssueRelationCreateInput!) { issueRelationCreate(input: $in) { success } }"
)


def finished_at(issue, finished):
    """The latest of issue's move into finished and its canceledAt and completedAt (the history
    read can lack a move into Duplicate that canceledAt holds); None: unknown."""
    times = [parse_time(issue[k]) for k in ("canceledAt", "completedAt") if issue[k]]
    if moved := last_move(issue["history"]["nodes"], finished):
        times.append(moved)
    return max(times, default=None)


class LinearTracker(Tracker):
    def __init__(self, gql, team, roles, humans):
        self.gql, self.team, self.roles, self.humans = gql, team, roles, humans
        self.others = {e.lower() for e in humans}
        self.users = {role: uid for uid, role in roles.items()}
        self.states = {team.states[k]: v for k, v in STATES.items()}
        self.ids = {v: team.states[k] for k, v in STATES.items()} | {
            IssueState.DONE: team.states[State.DONE],
            IssueState.CANCELED: team.states[State.CANCELED],
        }

    def _state(self, sid):
        found = self.states.get(sid)
        if found not in (IssueState.READY, IssueState.RUNNING) and sid in self.team.finished:
            return IssueState.DONE if sid == self.team.states[State.DONE] else IssueState.CANCELED
        return found

    def read(self, ident):
        issue = self.gql(Q_ISSUE, i=ident)["issue"]
        if not issue:
            return None
        state = issue["state"]["id"]
        found = self._state(state)
        if found in (IssueState.DONE, IssueState.CANCELED):
            return Snapshot(found, finished_at(issue, self.team.finished))
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

    def approved(self, owners):
        nodes = self.gql(
            Q_HANDOFF,
            t=self.team.id,
            s=self.ids[IssueState.APPROVED],
            a=[uid for uid, role in self.roles.items() if role in owners],
        )["issues"]["nodes"]
        return [
            Approved(
                n["id"],
                n["identifier"],
                n["url"],
                n["title"],
                n["priority"],
                n["createdAt"],
                n["project"]["id"],
                n["project"]["name"],
                self.roles[n["assignee"]["id"]],
                tuple(
                    (a["title"], a["url"])
                    for a in n["attachments"]["nodes"]
                    if not run_comments.is_record(a)
                ),
            )
            for n in nodes
        ]

    def detail(self, key):
        issue = self.gql(Q_DETAIL, i=key)["issue"]
        return Detail(
            self._state(issue["state"]["id"]),
            tuple(
                Move(h["createdAt"], self._state(h["toStateId"]), self._state(h["fromStateId"]))
                for h in issue["history"]["nodes"]
            ),
            tuple(
                Comment(
                    c["createdAt"],
                    c["body"],
                    (c["user"] or {}).get("name", "integration"),
                    other=bool(c["user"]) and (c["user"].get("email") or "").lower() in self.others,
                    run=run_comments.is_comment(c),
                )
                for c in issue["comments"]["nodes"]
            ),
            frozenset(r["relatedIssue"]["id"] for r in issue["relations"]["nodes"])
            | {r["issue"]["id"] for r in issue["inverseRelations"]["nodes"]},
        )

    def created(self, once):
        nodes = self.gql(Q_CHILD, c=client_id(*once))["issues"]["nodes"]
        return Ref(nodes[0]["id"], nodes[0]["identifier"]) if nodes else None

    def create(self, once, *, project, owner, priority, title, description):
        made = call(
            self.gql,
            M_CREATE,
            "issueCreate",
            **{
                "in": {
                    "id": client_id(*once),
                    "teamId": self.team.id,
                    "projectId": project,
                    "assigneeId": self.users[owner],
                    "stateId": self.ids[IssueState.READY],
                    "priority": priority,
                    "title": title,
                    "description": description,
                }
            },
        )["issue"]
        return Ref(made["id"], made["identifier"])

    def relate(self, key, other):
        call(
            self.gql,
            M_RELATE,
            "issueRelationCreate",
            **{"in": {"type": "related", "issueId": key, "relatedIssueId": other}},
        )

    def comment(self, key, body):
        ops.comment(self.gql, key, body)

    def move(self, key, to, *, frm):
        return ops.move(self.gql, key, self.ids[to], self.ids[frm])

    def comment_and_move(self, key, body, *, to, frm, notify):
        return ops.comment_and_move(
            self.gql,
            key,
            body,
            to=self.ids[to],
            frm=self.ids[frm],
            emails=self.humans if notify else (),
        )


def _sub_issues(nodes):
    return tuple(SubIssue(n["id"], n["identifier"], n["state"]["type"] in DONE) for n in nodes)
