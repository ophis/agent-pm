"""The engine Tracker on a local board (docs/board.md › Local board): its reads, and its writes, each
one locked rewrite of one issue file; promote's and prune's writes are TASK-795's."""

import os
import re
import stat
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from agent_pm.orchestrator.engine.flow.handoff import parse_handoff
from agent_pm.orchestrator.engine.flow.tracker import Tracker
from agent_pm.orchestrator.engine.flow.types import (
    Approved,
    ArchivePlan,
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
    Origin,
    Pick,
    Recheck,
    Snapshot,
    SubIssue,
)
from agent_pm.orchestrator.engine.types import UUID_RE, IssueError, TrackerFileError
from agent_pm.orchestrator.trackers.local import issue_file, store
from agent_pm.orchestrator.trackers.local.board import read_one, read_text, scan
from agent_pm.orchestrator.trackers.local.types import Entry, LocalState, Source

ISSUE_STATES = {
    LocalState.BACKLOG: IssueState.BACKLOG,
    LocalState.READY: IssueState.READY,
    LocalState.RUNNING: IssueState.RUNNING,
    LocalState.REVIEW: IssueState.REVIEW,
    LocalState.APPROVED: IssueState.APPROVED,
    LocalState.DONE: IssueState.DONE,
    LocalState.CANCELED: IssueState.CANCELED,
}
LOCAL_STATES = {issue: local for local, issue in ISSUE_STATES.items()}
ORIGINS: dict[Source, Origin] = {Source.SELF: "self", Source.OTHER: "other"}
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
TRIES = 3


class LocalTracker(Tracker):
    """board: its realpath'd dir, scanned once on first use; roles: the role dirs listed; now: the
    time writes record. Keys and identifiers are front-matter ids."""

    def __init__(
        self,
        board: str,
        roles: Collection[str],
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.board, self.roles, self.files, self.now = board, frozenset(roles), None, now

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

    def moved_at(self, key, state, *, by=None):
        found = self._all().get(key)
        return None if found is None else _last(found, state, by)

    def read(self, ident):
        found = self._all().get(ident)
        return None if found is None else Snapshot(_state(found.state), _last(found, found.state))

    def issue(self, ident):
        found = self._need(ident)
        files = self._all()
        notes = (
            Note(n.body, _stamp(n.at), None, n.source.value, ORIGINS[n.source])
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
        return [
            Finished(each.id, each.id, _last(each, each.state))
            for each in self._in(LocalState.DONE, LocalState.CANCELED)
            if each.state == LocalState.CANCELED or each.assignee in owners
        ]

    def archive_plan(self, key):
        """Unless archived, its unarchived descendants level by level."""
        found = self._need(key)
        archived = found.assignee is None
        below = () if archived else self._descendants(key)
        mine = (_sub_issue(found), *below)
        return ArchivePlan(
            found.id,
            found.id,
            _url(found),
            archived,
            below,
            () if archived else tuple(s for s in mine if not s.finished),
        )

    def _descendants(self, key):
        children, found, level, seen = self._children(), [], [key], {key}
        while level:
            below = [s for k in level for s in children.get(k, ()) if s.key not in seen]
            seen.update(s.key for s in below)
            found += below
            level = [s.key for s in below]
        return tuple(found)

    def _children(self):
        """Unarchived issues by parent id."""
        found = {}
        for each in self._all().values():
            if each.assignee is not None and each.parent:
                found[each.parent] = (*found.get(each.parent, ()), _sub_issue(each))
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
                    ORIGINS[n.source],
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
        wanted = issue_file.ref_key(ref)
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

    def _write_archive(self, key):
        _unsupported("archive")

    def _write(self, key, change):
        """Under the board lock, issue key's file read fresh and Stamped, then change(text, found):
        None, or (text, the state dir it moves to or None); written only when that differs from
        the Stamped text and the file still holds what was read. A file a person moved or deleted
        meanwhile is read again. Returns the file as the deciding change saw it."""
        with store.locked(self.board):
            for _ in range(TRIES):
                found = read_one(self.board, self._need(key).path, key)
                if found is None:
                    raise IssueError(f"{key}: issue not found")
                read = _read_if_there(found.path)
                if read is None:
                    continue
                text, mtime = read
                found = issue_file.parse(
                    found.path, text, assignee=found.assignee, state=found.state, mtime=mtime
                )
                stamped = issue_file.stamp(
                    text, mtime=mtime, state=found.state if found.assignee else None
                )
                changed = change(stamped, found)
                if changed is None or changed[0] == stamped:
                    self._all()[key] = found
                    return found
                again = _read_if_there(found.path)
                if again is None or again[0] != text:
                    continue
                path = found.path if changed[1] is None else self._rename(found, changed[1])
                if path is None:
                    continue
                store.replace(path, changed[0])
                text, mtime = read_text(path)
                self._all()[key] = issue_file.parse(
                    path,
                    text,
                    assignee=found.assignee,
                    state=changed[1] or found.state,
                    mtime=mtime,
                )
                return found
            raise TrackerFileError(found.path, "changed while writing")

    def _rename(self, issue, state):
        """issue's file moved, its name kept, into its assignee's state dir, made if absent; None:
        it was no longer there."""
        folder = os.path.join(self.board, issue.assignee, state.value)
        try:
            if not stat.S_ISDIR(os.lstat(folder).st_mode):
                raise TrackerFileError(folder, "not a directory")
        except FileNotFoundError:
            os.mkdir(folder)
        target = os.path.join(folder, os.path.basename(issue.path))
        if os.path.lexists(target):
            raise TrackerFileError(target, "file exists")
        try:
            os.rename(issue.path, target)
        except FileNotFoundError:
            if os.path.lexists(issue.path):
                raise
            return None
        return target

    def _move(self, key, to, frm, body=None):
        """None: moved (with body, a comment), or already in to with no frm; else its state."""

        def change(text, found):
            if found.assignee is None:
                raise IssueError(f"{key}: archived")
            if frm is not None and found.state != LOCAL_STATES[frm]:
                return None
            if found.state == LOCAL_STATES[to]:
                return None
            if body is not None:
                text = issue_file.add_comment(text, at=self.now(), body=body)
            return issue_file.add_history(text, at=self.now(), state=LOCAL_STATES[to]), (
                LOCAL_STATES[to]
            )

        state = self._write(key, change).state  # None only archived, which change refuses
        if state is None or frm is None or state == LOCAL_STATES[frm]:
            return None
        return state.value

    def move(self, key, to, *, frm=None):
        return self._move(key, to, frm)

    def claim(self, key):
        self._move(key, IssueState.RUNNING, None)

    def comment_and_move(self, key, body, *, to, frm, notify):
        """No one to notify on a local board."""
        return self._move(key, to, frm, body)

    def comment(self, key, body, ref=None):
        """A ref already posted: nothing."""
        wanted = None if ref is None else issue_file.ref_key(ref)

        def change(text, found):
            if wanted and any(n.source == Source.SELF and n.key == wanted for n in found.comments):
                return None
            return issue_file.add_comment(text, at=self.now(), body=body, ref_key=wanted), None

        self._write(key, change)

    def put_comment(self, key, body, ref):
        wanted = issue_file.ref_key(ref)

        def change(text, _):
            edited = issue_file.edit_comment(text, wanted, body)
            if edited is None:
                edited = issue_file.add_comment(text, at=self.now(), body=body, ref_key=wanted)
            return edited, None

        self._write(key, change)
        return True

    def notify(self, key, people):
        return ""

    def link(self, key, url, title):
        def change(text, found):
            if any(each.url == url for each in found.links):
                return None
            return issue_file.add_link(text, title=title, url=url), None

        self._write(key, change)

    def retitle(self, key, title):
        self._write(key, lambda text, _: (issue_file.set_title(text, title), None))

    def unarchive(self, key):
        """A live issue is on the board already; restoring an archived one is prune's."""
        if self._need(key).assignee is None:
            raise IssueError(f"{key}: archived")


def _read_if_there(path):
    """read_text's, or None when nothing is at path any more."""
    try:
        return read_text(path)
    except TrackerFileError:
        if os.path.lexists(path):
            raise
        return None


def _unsupported(name) -> NoReturn:
    raise NotImplementedError(f"local board: {name} is not supported yet")


def _sub_issue(issue):
    return SubIssue(issue.id, issue.id, _shown(issue), issue.state in ENDED)


def _url(issue):
    return Path(issue.path).as_uri()


def _stamp(at):
    """As Linear's times: UTC, milliseconds, Z."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _state(local):
    return None if local is None else ISSUE_STATES[local]


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


def _last(issue, state, by=None):
    """The latest move into state (by that origin when given); None if none."""
    times = [m.at for m in _moves(issue) if m.state == state and by in (None, m.source)]
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
