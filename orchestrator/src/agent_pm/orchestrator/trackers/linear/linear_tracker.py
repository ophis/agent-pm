"""The engine Tracker on Linear, from a gql callable: its states mapped to IssueState, its reads and
writes in Linear's query text (a run's issue read included), and which of its comments and
attachments are a run's."""

import functools
import re
from typing import TypeVar

from agent_pm.orchestrator.engine import config
from agent_pm.orchestrator.engine.flow.handoff import parse_handoff
from agent_pm.orchestrator.engine.flow.tracker import Tracker
from agent_pm.orchestrator.engine.flow.types import (
    UNREADABLE,
    Approved,
    Comment,
    Detail,
    Finished,
    Found,
    Group,
    Issue,
    IssueState,
    Link,
    Linked,
    Move,
    Note,
    Pick,
    Recheck,
    Ref,
    Snapshot,
    SubIssue,
)
from agent_pm.orchestrator.engine.types import UUID_RE, IssueError, LinearError, State
from agent_pm.orchestrator.trackers.linear import ops
from agent_pm.orchestrator.trackers.linear.ops import DONE, HISTORY, call, client_id, last_move
from agent_pm.utils import LOCAL, one_line, parse_time

T = TypeVar("T")

# Last wins where two keys share an id, so todo and in_progress skip it as before.
STATES = {
    State.IN_REVIEW: IssueState.REVIEW,
    State.HANDOFF: IssueState.APPROVED,
    State.TODO: IssueState.READY,
    State.IN_PROGRESS: IssueState.RUNNING,
}

LISTED = {IssueState.READY: State.TODO, IssueState.RUNNING: State.IN_PROGRESS}

MOVES = {**STATES, State.DONE: IssueState.DONE, State.CANCELED: IssueState.CANCELED}

URL = "https://agent-pm.invalid/run/"

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
RELATIONS = "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"
Q_RELATIONS = "query($i: String!) { issue(id: $i) { " + RELATIONS + " } }"
Q_RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } } }"
)
Q_HISTORY = "query($i: String!, $after: String) { issue(id: $i) { " + HISTORY + " } }"
_NODES = (
    "nodes { identifier title description state { id name type } "
    "attachments(first: 20) { nodes { url } } comments(first: 1, orderBy: createdAt) { nodes { body } } }"
)
_FOUND = f"searchIssues(term: $t, filter: $f, first: 10, includeArchived: true) {{ {_NODES} }}"
_NAMED = f"issues(filter: $n, first: 50, includeArchived: true) {{ {_NODES} }}"
Q_SEARCH = f"query($t: String!, $f: IssueFilter) {{ {_FOUND} }}"
Q_SEARCH_IDS = f"query($t: String!, $f: IssueFilter, $n: IssueFilter) {{ {_FOUND} {_NAMED} }}"
Q_IDS = f"query($n: IssueFilter) {{ {_NAMED} }}"
Q_UPSTREAM = "query($i: String!) { issue(id: $i) { identifier description } }"
GROUP_BY_STATE = {
    State.DONE: Group.DONE,
    State.IN_REVIEW: Group.IN_REVIEW,
    State.IN_PROGRESS: Group.IN_PROGRESS,
    State.TODO: Group.TODO,
    State.CANCELED: Group.CANCELED,
}
GROUP_BY_TYPE = {
    "completed": Group.DONE,
    "started": Group.IN_PROGRESS,
    "unstarted": Group.TODO,
    "canceled": Group.CANCELED,
}
Q_READ = """query($i: String!) { issue(id: $i) { id identifier url title description createdAt
  project { id }
  comments(first: 250) { nodes { body createdAt user { email name isMe } } }
  attachments(first: 50) { nodes { url } }
  labels(first: 50) { nodes { id } }
  relations(first: 50) { nodes { relatedIssue { identifier title state { name } } } }
  inverseRelations(first: 50) { nodes { issue { identifier title state { name } } } } } }"""


def q_issues(fields=""):
    return (
        (
            "query($f: IssueFilter) { issues(filter: $f, first: 100) { nodes { id identifier url priority createdAt "
            "project { id name } assignee { id email } "
        )
        + fields
        + " } } }"
    )


def pick(nodes, group, chosen, kind):
    """The choice of the issue's labels in group (none when group is empty): chosen maps a label id
    to its name; kind names the labels, "task" or "Agent"."""
    found = [n for n in nodes if group and (n["parent"] or {}).get("id") == group]
    if not found:
        return Pick()
    if len(found) > 1:
        names = ", ".join(n["name"] for n in found)
        return Pick(
            error=f"Several {kind} labels ({names}); keep one, then move the issue back to Todo."
        )
    if (name := chosen.get(found[0]["id"])) is None:
        return Pick(
            error=f'{kind[0].upper()}{kind[1:]} label "{found[0]["name"]}" is not in {LOCAL}\'s '
            f"[orchestrator.linear.{kind.lower()}_labels]. Fix the label, then move the issue back to Todo."
        )
    return Pick(name)


def blockers(issue):
    nodes = [n for n in issue["inverseRelations"]["nodes"] if n["type"] == "blocks"]
    return [
        n["issue"]["identifier"] if n.get("issue") else UNREADABLE
        for n in nodes
        if not n.get("issue") or n["issue"]["state"]["type"] not in DONE
    ]


def is_record(attachment):
    # the URL alone identifies one: a source may be titled "Run rate analysis"
    return attachment["url"].startswith(URL)


def is_comment(comment):
    # isMe means the harness account because callers query with its key
    return (
        bool((comment["user"] or {}).get("isMe"))
        and re.match(f"Run {UUID_RE.pattern} · ", comment["body"]) is not None
    )


def finished_at(issue, finished):
    """The latest of issue's move into finished and its canceledAt and completedAt (the history
    read can lack a move into Duplicate that canceledAt holds); None: unknown."""
    times = [parse_time(issue[k]) for k in ("canceledAt", "completedAt") if issue[k]]
    if moved := last_move(issue["history"]["nodes"], finished):
        times.append(moved)
    return max(times, default=None)


def _quiet(*_, **__):
    pass


def team_and_roles(gql, linear):
    """The team (states, finished) and the role accounts' user ids promote and prune build their
    tracker from, by the LinearSettings linear: the team read first."""
    return ops.team(gql, linear), ops.role_ids(gql, linear.roles)


class LinearTracker(Tracker):
    """states: {State key: state id}; team_id; finished: ops.Team.finished; roles: {user id: role};
    humans: human_members emails (comments' other, subscribed on notify); people: their user ids,
    which connect reads and moved_at's by_people needs; say: the caller's logger. A method needing
    one not given raises TypeError. service: the key each call is made as, with timeout when given; None:
    gql as given. A comment's ref: its client id's parts. A home is a Linear project; a note's account
    its user's email. task_group, agent_group: the task and Agent label groups; label_tasks,
    label_clients: {label id: task or client}, which recheck's picks read."""

    def __init__(
        self,
        gql,
        *,
        states=None,
        team_id=None,
        finished=None,
        roles=None,
        humans=(),
        people=frozenset(),
        say=_quiet,
        service=None,
        timeout=None,
        task_group=None,
        label_tasks=None,
        agent_group=None,
        label_clients=None,
    ):
        if service is not None:
            bound = {"service": service} | ({} if timeout is None else {"timeout": timeout})
            gql = functools.partial(gql, **bound)
        self.gql, self.state_ids, self.team_id, self.roles = gql, states, team_id, roles
        self.finished_ids, self.humans = finished, humans
        self.people, self.say, self.hist = people, say, {}
        self.task_group, self.label_tasks = task_group, label_tasks or {}
        self.agent_group, self.label_clients = agent_group, label_clients or {}
        self.others = {e.lower() for e in humans}
        self.users = {role: uid for uid, role in (roles or {}).items()}
        self.states = {states[k]: v for k, v in STATES.items()} if states else {}
        self.ids = {v: states[k] for k, v in MOVES.items()} if states else {}

    def _state(self, sid):
        found = self.states.get(sid)
        if found not in (IssueState.READY, IssueState.RUNNING) and sid in self.finished_ids:
            return IssueState.DONE if sid == self.ids[IssueState.DONE] else IssueState.CANCELED
        return found

    @classmethod
    def connect(cls, gql, cfg, *, say):
        """Checks the team and task label group, then reads the human and role accounts; keeps
        the task and Agent label maps."""
        linear = config.linear_settings(cfg, "router")
        found = ops.team(gql, linear)
        ops.task_group(gql, linear)
        people = frozenset(ops.humans(gql, linear))
        return cls(
            gql,
            states=found.states,
            team_id=found.id,
            finished=found.finished,
            roles=ops.role_ids(gql, linear.roles),
            humans=linear.human_members,
            people=people,
            say=say,
            task_group=linear.task_label_group,
            label_tasks={i: task for task, i in linear.task_labels.items()},
            agent_group=linear.agent_label_group,
            label_clients={i: client for client, i in linear.agent_labels.items()},
        )

    @classmethod
    def of_team(cls, gql, team, roles, *, humans=()):
        """From team_and_roles' reads."""
        return cls(
            gql,
            states=team.states,
            team_id=team.id,
            finished=team.finished,
            roles=roles,
            humans=humans,
        )

    def role(self, issue):
        return _need("roles", self.roles)[issue["assignee"]["id"]]

    def assigned(self, state):
        return self._listed(state)

    def _listed(self, state, fields=""):
        """state's issues with a project, assigned to a role account; the first 100."""
        if state not in LISTED:
            raise ValueError(f"no list of {state} issues")
        flt = {
            "team": {"id": {"eq": _need("team_id", self.team_id)}},
            "project": {"null": False},
            "assignee": {"id": {"in": list(_need("roles", self.roles))}},
            "state": {"id": {"eq": _need("states", self.state_ids)[LISTED[state]]}},
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

    def recheck(self, key):
        """ready: in the configured Todo state id, whatever other key shares it; refused without
        a project."""
        found = self.gql(Q_RECHECK, i=key)["issue"]
        ready = found["state"]["id"] == _need("states", self.state_ids)[State.TODO]
        nodes = found["labels"]["nodes"]
        return Recheck(
            ready,
            role=_need("roles", self.roles).get((found["assignee"] or {}).get("id")),
            refused=None if found["project"] else "no project",
            assignee=found["assignee"],
            home=found["project"],
            task=pick(nodes, self.task_group, self.label_tasks, "task"),
            client=pick(nodes, self.agent_group, self.label_clients, "Agent"),
        )

    def moved_at(self, key, state, *, by_people=False):
        """History read once per key; by_people: moves by the human members only."""
        if key not in self.hist:
            self.hist[key] = self.gql(Q_HISTORY, i=key)["issue"]["history"]["nodes"]
        moved = {_need("states", self.state_ids)[LISTED[state]]}
        return last_move(self.hist[key], moved, self.people if by_people else None)

    def read(self, ident):
        _need("states", self.state_ids)
        ended = _need("finished", self.finished_ids)
        issue = self.gql(Q_ISSUE, i=ident)["issue"]
        if not issue:
            return None
        state = issue["state"]["id"]
        found = self._state(state)
        if found in (IssueState.DONE, IssueState.CANCELED):
            return Snapshot(found, finished_at(issue, ended))
        return Snapshot(found, last_move(issue["history"]["nodes"], {state}))

    def issue(self, ident):
        node = self.gql(Q_READ, i=ident)["issue"]
        if node is None:
            raise IssueError(f"{ident}: issue not found")
        if node["identifier"] != ident:
            raise IssueError(f"Linear returned {str(node['identifier'])[:40]!r} for {ident}")
        notes = sorted(
            (
                Note(
                    c["body"],
                    c["createdAt"],
                    (c["user"] or {}).get("email"),
                    (c["user"] or {}).get("name"),
                )
                for c in node["comments"]["nodes"]
                if not is_comment(c)
            ),
            key=lambda n: parse_time(n.at),
        )
        linked = {}
        for r in [x["relatedIssue"] for x in node["relations"]["nodes"]] + [
            x["issue"] for x in node["inverseRelations"]["nodes"]
        ]:
            linked.setdefault(
                r["identifier"], Linked(r["identifier"], r["title"], r["state"]["name"])
            )
        description = node["description"] or ""
        return Issue(
            id=node["id"],
            identifier=node["identifier"],
            url=node["url"],
            title=node["title"],
            description=description,
            handoff=parse_handoff(description),
            created_at=node["createdAt"],
            home=(node["project"] or {}).get("id"),
            notes=tuple(notes),
            links=tuple(Link(a["url"]) for a in node["attachments"]["nodes"]),
            linked=tuple(linked.values()),
            labels=tuple(n["id"] for n in node["labels"]["nodes"]),
        )

    def search(self, words, idents):
        if not words and not idents:
            return ()
        f = {"team": {"id": {"eq": _need("team_id", self.team_id)}}}
        n = {**f, "number": {"in": [int(i.rsplit("-", 1)[1]) for i in idents]}}
        if words and idents:
            data = self.gql(Q_SEARCH_IDS, t=" ".join(words), f=f, n=n)
        elif words:
            data = self.gql(Q_SEARCH, t=" ".join(words), f=f)
        else:
            data = self.gql(Q_IDS, n=n)
        nodes = [
            *data.get("searchIssues", {"nodes": []})["nodes"],
            *data.get("issues", {"nodes": []})["nodes"],
        ]
        by_id = {_need("states", self.state_ids)[k]: g for k, g in GROUP_BY_STATE.items()}
        return tuple(_found(n, by_id) for n in nodes)

    def description(self, ident):
        try:
            node = self.gql(Q_UPSTREAM, i=ident)["issue"]
        except LinearError as e:
            if not ops.not_found(e):
                raise
            return None
        return None if node is None else node["description"] or ""

    def finished(self, owners):
        states, team = _need("states", self.state_ids), _need("team_id", self.team_id)
        roles, ended = _need("roles", self.roles), _need("finished", self.finished_ids)
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

    def approved(self, owners):
        _need("states", self.state_ids)
        roles = _need("roles", self.roles)
        nodes = self.gql(
            Q_HANDOFF,
            t=_need("team_id", self.team_id),
            s=self.ids[IssueState.APPROVED],
            a=[uid for uid, role in roles.items() if role in owners],
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
                roles[n["assignee"]["id"]],
                tuple(
                    (a["title"], a["url"]) for a in n["attachments"]["nodes"] if not is_record(a)
                ),
            )
            for n in nodes
        ]

    def detail(self, key):
        _need("states", self.state_ids)
        _need("finished", self.finished_ids)
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
                    run=is_comment(c),
                )
                for c in issue["comments"]["nodes"]
            ),
            frozenset(r["relatedIssue"]["id"] for r in issue["relations"]["nodes"])
            | {r["issue"]["id"] for r in issue["inverseRelations"]["nodes"]},
        )

    def created(self, once):
        nodes = self.gql(Q_CHILD, c=client_id(*once))["issues"]["nodes"]
        return Ref(nodes[0]["id"], nodes[0]["identifier"]) if nodes else None

    def create(self, once, *, home, owner, priority, title, description):
        _need("states", self.state_ids)
        made = call(
            self.gql,
            M_CREATE,
            "issueCreate",
            **{
                "in": {
                    "id": client_id(*once),
                    "teamId": _need("team_id", self.team_id),
                    "projectId": home,
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

    def comment(self, key, body, ref=None):
        ops.comment(self.gql, key, body, None if ref is None else client_id(*ref))

    def move(self, key, to, *, frm=None):
        _need("states", self.state_ids)
        if frm is not None:
            return ops.move(self.gql, key, self.ids[to], self.ids[frm])
        if self.gql(ops.Q_ISSUE_STATE, i=key)["issue"]["state"]["id"] != self.ids[to]:
            call(self.gql, ops.M_STATE, "issueUpdate", i=key, s=self.ids[to])
        return None

    def claim(self, key):
        _need("states", self.state_ids)
        call(self.gql, ops.M_STATE, "issueUpdate", i=key, s=self.ids[IssueState.RUNNING])

    def comment_and_move(self, key, body, *, to, frm, notify):
        _need("states", self.state_ids)
        return ops.comment_and_move(
            self.gql,
            key,
            body,
            to=self.ids[to],
            frm=self.ids[frm],
            emails=self.humans if notify else (),
        )

    def notify(self, key, people):
        return ops.subscribe(self.gql, key, people)

    def links(self, key):
        return {a["url"] for a in self.gql(Q_ATTACHED, i=key)["issue"]["attachments"]["nodes"]}

    def link(self, key, url, title):
        try:
            call(self.gql, M_ATTACH, "attachmentLinkURL", i=key, u=url, t=title)
        except LinearError as e:
            if not any(s in str(e) for s in ATTACHED):  # the GitHub integration links PRs itself
                raise

    def retitle(self, key, title):
        call(self.gql, M_TITLE, "issueUpdate", i=key, t=title)

    def on_board(self, ident):
        issue = self.gql(Q_ID, i=ident)["issue"]
        return issue["id"] if issue and issue["team"]["id"] == self.team_id else None

    def unarchive(self, key):
        call(self.gql, M_UNARCHIVE, "issueUnarchive", i=key)

    def find_comment(self, key, ref):
        return ops.find_comment(self.gql, client_id(*ref), key)

    def put_comment(self, key, body, ref):
        cid = client_id(*ref)
        try:
            done = self.gql(M_UPDATE, c=cid, b=body)["commentUpdate"]
        except LinearError as e:
            if not ops.not_found(e):
                raise
            done = None
        if done is None:
            done = self.gql(ops.M_COMMENT_ID, c=cid, i=key, b=body)["commentCreate"]
        return done["success"]


def _need(name: str, value: T | None) -> T:
    if value is None:
        raise TypeError(f"LinearTracker needs {name}")
    return value


def _sub_issues(nodes):
    return tuple(SubIssue(n["id"], n["identifier"], n["state"]["type"] in DONE) for n in nodes)


def _found(n, by_id):
    st, comments = n["state"], n["comments"]["nodes"]
    group = by_id.get(st["id"], GROUP_BY_TYPE.get(st["type"], Group.BACKLOG))
    return Found(
        n["identifier"],
        n["title"],
        st["name"],
        group,
        tuple(a["url"] for a in n["attachments"]["nodes"]),
        n["description"] or "",
        comments[0]["body"] if group == Group.CANCELED and comments else "",
    )
