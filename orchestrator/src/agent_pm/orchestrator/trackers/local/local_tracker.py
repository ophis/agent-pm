"""The engine Tracker on a local board (docs/board.md › Local board): its read side; its writes are
TASK-794's."""

import re
from collections.abc import Collection
from datetime import UTC
from pathlib import Path
from typing import NoReturn

from agent_pm.orchestrator.engine.flow.handoff import parse_handoff
from agent_pm.orchestrator.engine.flow.tracker import Tracker
from agent_pm.orchestrator.engine.flow.types import (
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
    Snapshot,
    SubIssue,
)
from agent_pm.orchestrator.engine.types import UUID_RE, IssueError
from agent_pm.orchestrator.trackers.local.board import read_one, scan
from agent_pm.orchestrator.trackers.local.types import Entry, LocalState, Source

ISSUE_STATES = {
    LocalState.READY: IssueState.READY,
    LocalState.RUNNING: IssueState.RUNNING,
    LocalState.REVIEW: IssueState.REVIEW,
    LocalState.APPROVED: IssueState.APPROVED,
    LocalState.DONE: IssueState.DONE,
    LocalState.CANCELED: IssueState.CANCELED,
}
LOCAL_STATES = {issue: local for local, issue in ISSUE_STATES.items()}
ENDED = frozenset({LocalState.DONE, LocalState.CANCELED})
LISTED = frozenset({IssueState.READY, IssueState.RUNNING})
GROUPS = {
    LocalState.DONE: Group.DONE,
    LocalState.REVIEW: Group.IN_REVIEW,
    LocalState.RUNNING: Group.IN_PROGRESS,
    LocalState.APPROVED: Group.IN_PROGRESS,
    LocalState.READY: Group.TODO,
    LocalState.CANCELED: Group.CANCELED,
}
RUN = re.compile(f"Run {UUID_RE.pattern} · ")
ARCHIVED = "archived"
SEARCHED = 10


class LocalTracker(Tracker):
    """board: its realpath'd dir, scanned once on first use; roles: the role dirs listed. Keys and
    identifiers are front-matter ids."""

    def __init__(self, board: str, roles: Collection[str]):
        self.board, self.roles, self.files = board, frozenset(roles), None

    def _all(self):
        if self.files is None:
            self.files = {each.id: each for each in scan(self.board)}
        return self.files

    def _need(self, ident):
        found = self._all().get(ident)
        if found is None:
            raise IssueError(f"{ident}: issue not found")
        return found

    def _in(self, *states, owners=None):
        """Role dirs' (owners' when given) issues located in states."""
        return [
            each
            for each in self._all().values()
            if each.assignee in self.roles
            and (owners is None or each.assignee in owners)
            and each.state in states
        ]

    def _related(self, issue):
        """Own related and blocked_by ids, then the issues naming it in theirs."""
        naming = [
            each.id
            for each in self._all().values()
            if issue.id in each.related or issue.id in each.blocked_by
        ]
        return list(dict.fromkeys([*issue.related, *issue.blocked_by, *naming]))

    def role(self, issue):
        return issue["assignee"]["id"]

    def assigned(self, state):
        if state not in LISTED:
            raise ValueError(f"no list of {state} issues")
        return [_record(each) for each in self._in(LOCAL_STATES[state])]

    def ready(self):
        """An id with no issue file blocks."""
        files = self._all()
        return [
            (
                _record(each),
                tuple(b for b in each.blocked_by if b not in files or files[b].state not in ENDED),
            )
            for each in self._in(LocalState.READY)
        ]

    def recheck(self, key):
        known = self._all().get(key)
        found = read_one(self.board, known.path, key) if known else None
        if found is None:
            return Recheck(
                ready=False,
                role=None,
                refused=None,
                assignee=None,
                home=None,
                task=Pick(),
                client=Pick(),
            )
        self._all()[key] = found
        role = found.assignee if found.assignee in self.roles else None
        return Recheck(
            ready=role is not None and found.state == LocalState.READY,
            role=role,
            refused=None,
            assignee=None if found.assignee is None else {"id": found.assignee},
            home=None,
            task=Pick(found.task),
            client=Pick(found.client),
        )

    def moved_at(self, key, state, *, by_people=False):
        found = self._all().get(key)
        return None if found is None else _last(found, state, Source.OTHER if by_people else None)

    def read(self, ident):
        found = self._all().get(ident)
        return None if found is None else Snapshot(_state(found.state), _last(found, found.state))

    def issue(self, ident):
        found = self._need(ident)
        files = self._all()
        notes = (
            Note(n.body, _stamp(n.at), None, n.source.value)
            for n in found.comments
            if not _is_run(n)
        )
        return Issue(
            id=found.id,
            identifier=found.id,
            url=_url(found),
            title=found.title,
            description=found.description,
            created_at=_stamp(found.created),
            home=None,
            notes=tuple(sorted(notes, key=lambda n: n.at)),
            links=tuple(Link(each.url) for each in found.links),
            linked=tuple(
                Linked(i, files[i].title, _shown(files[i]))
                for i in self._related(found)
                if i in files
            ),
            handoff=parse_handoff(found.description),
        )

    def search(self, words, idents):
        files = self._all().values()
        lower = [w.lower() for w in words]
        matched = [
            each
            for each in files
            if lower
            and all(w in each.title.lower() or w in each.description.lower() for w in lower)
        ]
        named = [each for each in files if each.id in idents]
        return tuple(map(_found, named + matched[:SEARCHED]))

    def description(self, ident):
        found = self._all().get(ident)
        return None if found is None else found.description

    def finished(self, owners):
        children = self._children()
        return [
            Finished(each.id, each.id, _last(each, each.state), children.get(each.id, ()))
            for each in self._in(LocalState.DONE, LocalState.CANCELED)
            if each.state == LocalState.CANCELED or each.assignee in owners
        ]

    def sub_issues(self, key):
        return self._children().get(key, ())

    def _children(self):
        """Unarchived issues by parent id."""
        found = {}
        for each in self._all().values():
            if each.assignee is not None and each.parent:
                found[each.parent] = (
                    *found.get(each.parent, ()),
                    SubIssue(each.id, each.id, each.state in ENDED),
                )
        return found

    def approved(self, owners):
        return [
            Approved(
                each.id,
                each.id,
                _url(each),
                each.title,
                each.priority,
                each.created.isoformat(),
                "",
                "",
                each.assignee or "",
                tuple((link.title, link.url) for link in each.links),
            )
            for each in self._in(LocalState.APPROVED, owners=owners)
        ]

    def detail(self, key):
        found = self._need(key)
        moves, frm = [], None
        for move in _moves(found):
            moves.append(Move(_stamp(move.at), _state(move.state), frm))
            frm = moves[-1].to
        return Detail(
            _state(found.state),
            tuple(moves),
            tuple(
                Comment(
                    _stamp(n.at),
                    n.body,
                    n.source.value,
                    other=n.source == Source.OTHER,
                    run=_is_run(n),
                )
                for n in found.comments
            ),
            frozenset(self._related(found)),
        )

    def links(self, key):
        return {each.url for each in self._need(key).links}

    def on_board(self, ident):
        return ident if ident in self._all() else None

    def find_comment(self, key, ref):
        wanted = ":".join(ref)
        return next(
            (
                n.body
                for n in self._need(key).comments
                if n.source == Source.SELF and n.key == wanted
            ),
            None,
        )

    def created(self, once):
        _unsupported("created")

    def create(self, once, *, home, owner, priority, title, description):
        _unsupported("create")

    def relate(self, key, other):
        _unsupported("relate")

    def comment(self, key, body, ref=None):
        _unsupported("comment")

    def move(self, key, to, *, frm=None):
        _unsupported("move")

    def claim(self, key):
        _unsupported("claim")

    def comment_and_move(self, key, body, *, to, frm, notify):
        _unsupported("comment_and_move")

    def notify(self, key, people):
        _unsupported("notify")

    def link(self, key, url, title):
        _unsupported("link")

    def retitle(self, key, title):
        _unsupported("retitle")

    def archive(self, key):
        _unsupported("archive")

    def unarchive(self, key):
        _unsupported("unarchive")

    def put_comment(self, key, body, ref):
        _unsupported("put_comment")


def _unsupported(name) -> NoReturn:
    raise NotImplementedError(f"local board: {name} is not supported yet")


def _url(issue):
    return Path(issue.path).as_uri()


def _stamp(at):
    """As Linear's times: UTC, milliseconds, Z."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _state(local):
    return ISSUE_STATES.get(local) if local else None


def _shown(issue):
    return issue.state.value if issue.state else ARCHIVED


def _record(issue):
    return {
        "id": issue.id,
        "identifier": issue.id,
        "url": _url(issue),
        "title": issue.title,
        "priority": issue.priority,
        "createdAt": _stamp(issue.created),
        "assignee": {"id": issue.assignee},
        "project": None,
    }


def _moves(issue):
    """History, then the derived move: a person's move to where the file sits, unrecorded."""
    moves = list(issue.history)
    if issue.state is not None and (not moves or moves[-1].state != issue.state):
        moves.append(Entry(issue.mtime, issue.state, Source.OTHER))
    return moves


def _last(issue, state, source=None):
    """The latest move into state (by source when given); None if none."""
    times = [m.at for m in _moves(issue) if m.state == state and source in (None, m.source)]
    return times[-1] if times else None


def _is_run(note):
    return note.source == Source.SELF and RUN.match(note.body) is not None


def _found(issue):
    last = max(issue.comments, key=lambda n: n.at).body if issue.comments else ""
    return Found(
        issue.id,
        issue.title,
        _shown(issue),
        GROUPS.get(issue.state, Group.BACKLOG),
        tuple(each.url for each in issue.links),
        issue.description,
        last if issue.state == LocalState.CANCELED else "",
    )
