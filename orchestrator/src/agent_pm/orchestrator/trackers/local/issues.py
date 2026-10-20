"""The issue command's backend on a local board: Linear's output shapes with local values
(docs/board.md › Local board), writing as a person (other)."""

import re
from collections.abc import Mapping
from datetime import datetime

from agent_pm.orchestrator.engine.flow.types import (
    BOTH_DIRECTIONS,
    IDENT_ERROR,
    ISSUE_ID,
    PRIORITY_NUMBERS,
)
from agent_pm.orchestrator.engine.types import (
    UUID_RE,
    IssueError,
    RelationsFailed,
    Role,
    TrackerFileError,
)
from agent_pm.orchestrator.trackers.local import issue_file
from agent_pm.orchestrator.trackers.local.local_tracker import (
    ARCHIVED,
    UNASSIGNED,
    LocalTracker,
    archived_from,
    stamp_time,
    url,
)
from agent_pm.orchestrator.trackers.local.types import LocalState, Source

ACTOR = {"id": "other", "name": "other"}
TYPES = {
    LocalState.BACKLOG: "backlog",
    LocalState.READY: "unstarted",
    LocalState.RUNNING: "started",
    LocalState.REVIEW: "started",
    LocalState.APPROVED: "started",
    LocalState.DONE: "completed",
    LocalState.CANCELED: "canceled",
}
RELATION = "agent-pm-orchestrator issue relation add"
ERROR_CHARS = 200
_BAD_CLIENT = re.compile(r'^"|[\s\x00-\x1f\x7f-\x9f]')


class LocalIssues:
    def __init__(
        self, board, roles: Mapping[str, Role], *, now=lambda: datetime.now().astimezone()
    ):
        self.tracker = LocalTracker(board, roles, now=now)
        self.roles = list(roles)
        self.tasks = list(dict.fromkeys(task for role in roles.values() for task in role.tasks))
        self.tracker.files()

    def _issue(self, ident, option=None):
        where = "" if option is None else f"{option} "
        if not (re.fullmatch(ISSUE_ID, ident) or UUID_RE.fullmatch(ident)):
            raise IssueError(where + IDENT_ERROR.format(ident=ident))
        found = self.tracker.files().get(ident)
        if found is None:
            raise IssueError(f"{where}{ident}: no such issue")
        return found

    def _folder(self, spec):
        if spec in self.roles or spec in ("me", UNASSIGNED):
            return spec
        raise IssueError(f"--role {spec}: not a role: {', '.join([*self.roles, 'me', UNASSIGNED])}")

    def _names(self, args):
        if args.task is not None and args.task not in self.tasks:
            raise IssueError(f"--task {args.task}: not a configured task: {', '.join(self.tasks)}")
        if args.client is not None and (not args.client or _BAD_CLIENT.search(args.client)):
            raise IssueError(
                f"--client {args.client!r}: want a client name: no whitespace, control character "
                "or leading quote"
            )

    def view(self, ident, *, full):
        """Always whole: full changes nothing."""
        found, files = self._issue(ident), self.tracker.files()
        state, person = found.state, _person(found)
        notes = [(n, note) for n, note in enumerate(found.comments, 1) if not _archive_note(note)]
        notes.sort(key=lambda p: (p[1].at, p[0]), reverse=True)
        return {
            "id": found.id,
            "identifier": found.id,
            "title": found.title,
            "url": url(found),
            "state": None
            if state is None
            else {"id": state.value, "name": state.value, "type": TYPES[state]},
            "labels": _labels(found),
            "project": None,
            "milestone": None,
            "assignee": None if person is None else {**person, "email": None},
            "description": found.description,
            "comments": [_comment(found.id, n, note) for n, note in notes],
            "blocks": self._column(i for i, each in files.items() if found.id in each.blocked_by),
            "blocked_by": self._column(found.blocked_by),
            "parent": None if found.parent is None else self._column([found.parent])[0],
            "children": self._column(
                i for i, each in files.items() if each.assignee and each.parent == found.id
            ),
            "truncated": [],
            "continue": None,
        }

    def _column(self, idents):
        files, column = self.tracker.files(), []
        for ident in sorted(idents, key=_number):
            found = files.get(ident)
            title = None if found is None else found.title
            state = None if found is None or found.state is None else found.state.value
            column.append({"id": ident, "identifier": ident, "title": title, "state": state})
        return column

    def page(self, args, first, cursor):
        rows = self.walk(args)
        if cursor is not None:
            at = next((n for n, row in enumerate(rows) if row[2] == cursor), None)
            if at is None:
                raise IssueError(f"--cursor {cursor}: not a cursor of this list")
            rows = rows[at + 1 :]
        page = rows[:first]
        return page, len(rows) > first, page[-1][2] if page else None

    def walk(self, args):
        keep = self._filters(args)
        found = [each for each in self.tracker.files().values() if all(k(each) for k in keep)]
        found.sort(key=lambda each: (each.mtime, _number(each.id)), reverse=True)
        return [(_item(each), [], each.id) for each in found]

    def _filters(self, args):
        self._names(args)
        keep = [] if args.include_archived else [lambda each: each.assignee is not None]
        if args.query:
            text = args.query.lower()
            keep.append(lambda each: text in each.title.lower() or text in each.description.lower())
        if args.state is not None:
            keep.append(lambda each: each.state == args.state)
        if args.role is not None:
            folder = self._folder(args.role)
            keep.append(lambda each: _dir(each) == folder)
        if args.task is not None:
            keep.append(lambda each: each.task == args.task)
        if args.client is not None:
            keep.append(lambda each: each.client == args.client)
        if args.priority is not None:
            keep.append(lambda each: each.priority == PRIORITY_NUMBERS[args.priority])
        if args.parent is not None:
            parent = self._issue(args.parent, "--parent").id
            keep.append(lambda each: each.parent == parent)
        return keep

    def create(self, args, body):
        owner = self._folder(UNASSIGNED if args.role is None else args.role)
        self._names(args)
        priority = 0 if args.priority is None else PRIORITY_NUMBERS[args.priority]
        parent = None if args.parent is None else self._issue(args.parent, "--parent").id
        picked = {}
        for option, values in (("--blocks", args.blocks), ("--blocked-by", args.blocked_by)):
            for value in dict.fromkeys(values):
                found = self._issue(value, option)
                if picked.setdefault(found.id, option) != option:
                    raise IssueError(BOTH_DIRECTIONS.format(identifier=found.id))
        state = LocalState(args.state or LocalState.BACKLOG)

        def render(ident):
            return issue_file.new(
                ident=ident,
                title=args.title,
                priority=priority,
                created=self.tracker.now(),
                parent=parent,
                description=body,
                state=state,
                task=args.task,
                client=args.client,
                blocked_by=[i for i, option in picked.items() if option == "--blocked-by"],
                source=Source.OTHER,
            )

        key = self.tracker.add(owner, state, args.title, render).key
        done, failed = [], []
        for other, option in picked.items():
            if option == "--blocked-by":
                done.append({"blocked_by": other})
                continue
            try:
                self._write(other, _blocked(key, add=True))
            except (IssueError, TrackerFileError, OSError) as error:
                reason = (str(error) or type(error).__name__)[:ERROR_CHARS]
                failed.append({"blocks": other, "error": reason})
            else:
                done.append({"blocks": other})
        receipt = self._receipt(key, "create", "changed")
        if failed:
            recover = [f"{RELATION} {key} --blocks {each['blocks']}" for each in failed]
            report = {**receipt, "result": "partial", "done": done, "failed": failed}
            raise RelationsFailed({**report, "recover": recover})
        return receipt

    def edit(self, args, body):
        self._names(args)
        key = self._issue(args.id).id

        def change(text, found):
            edited = text
            if args.title is not None and args.title != found.title:
                edited = issue_file.set_title(edited, args.title)
            if body is not None and body.strip() != found.description:
                edited = issue_file.set_description(edited, body)
            if args.task is not None and args.task != found.task:
                edited = issue_file.set_front(edited, "task", args.task)
            if args.client is not None and args.client != found.client:
                edited = issue_file.set_front(edited, "client", args.client)
            priority = None if args.priority is None else PRIORITY_NUMBERS[args.priority]
            if priority is not None and priority != found.priority:
                edited = issue_file.set_front(edited, "priority", priority)
            return None if edited == text else (edited, None)

        return self._receipt(key, "edit", self._write(key, change))

    def move(self, ident, state):
        key, target = self._issue(ident).id, LocalState(state)

        def change(text, found):
            if found.assignee is None:
                raise IssueError(f"{key}: archived")
            if found.state == target:
                return None
            now = self.tracker.now()
            moved = issue_file.add_history(text, at=now, state=target, source=Source.OTHER)
            return moved, (found.assignee, target)

        return self._receipt(key, "move", self._write(key, change))

    def comment(self, ident, body):
        key = self._issue(ident).id

        def change(text, _):
            now = self.tracker.now()
            return issue_file.add_comment(text, at=now, body=body, source=Source.OTHER), None

        self.tracker.write(key, change)
        count = len(self.tracker.files()[key].comments)
        return self._receipt(key, "comment", "changed", comment_id=f"{key}#{count}")

    def relation(self, op, ident, direction, other):
        option = "--" + direction.replace("_", "-")
        node = self._issue(ident).id
        target = self._issue(other, option).id
        if target == node:
            raise IssueError(f"{option} {other}: {ident} cannot block itself")
        blocker, blocked = (node, target) if direction == "blocks" else (target, node)
        return self._receipt(
            node, f"relation {op}", self._write(blocked, _blocked(blocker, add=op == "add"))
        )

    def archive_plan(self, ident):
        return self.tracker.archive_plan(self._issue(ident).id)

    def archive(self, ident):
        key = self._issue(ident).id
        changed = self.tracker.archive(key, self.tracker.archive_plan(key))
        return self._receipt(key, "archive", "changed" if changed else "unchanged")

    def _write(self, key, change):
        """tracker.write(key, change); changed when change's last call wrote."""
        wrote = []

        def step(text, found):
            changed = change(text, found)
            wrote.append(changed is not None)
            return changed

        self.tracker.write(key, step)
        return "changed" if wrote[-1] else "unchanged"

    def _receipt(self, key, op, result, **extra):
        issue = {"id": key, "identifier": key, "url": url(self.tracker.files()[key])}
        return {"issue": issue, "operation": op, "result": result, "actor": dict(ACTOR), **extra}


def _number(ident):
    return int(ident.rsplit("-", 1)[1]), ident


def _dir(found):
    return archived_from(found) if found.assignee is None else found.assignee


def _person(found):
    folder = _dir(found)
    return None if folder == UNASSIGNED else {"id": folder, "name": folder}


def _labels(found):
    return [{"id": name, "name": name} for name in (found.task, found.client) if name]


def _archive_note(note):
    """The engine's bookkeeping, which Linear has no comment for."""
    return note.source == Source.SELF and (note.key or "").startswith(f"{ARCHIVED}:")


def _comment(ident, n, note):
    return {
        "id": f"{ident}#{n}",
        "author": {"id": note.source.value, "name": note.source.value},
        "created_at": stamp_time(note.at),
        "body": note.body,
        "parent_id": None,
        "quoted_text": None,
        "on_behalf_of": None,
    }


def _item(found):
    state = found.state
    return {
        "id": found.id,
        "identifier": found.id,
        "title": found.title,
        "url": url(found),
        "state": None if state is None else {"id": state.value, "name": state.value},
        "labels": _labels(found),
        "assignee": _person(found),
        "project": None,
        "parent_id": found.parent,
    }


def _blocked(blocker, *, add):
    """A write's change adding blocker to an issue's blocked_by, or removing it."""

    def change(text, found):
        if (blocker in found.blocked_by) == add:
            return None
        ids = [*found.blocked_by, blocker] if add else [i for i in found.blocked_by if i != blocker]
        return issue_file.set_front(text, "blocked_by", ids), None

    return change
