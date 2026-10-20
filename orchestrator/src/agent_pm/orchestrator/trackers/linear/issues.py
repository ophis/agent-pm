"""The issue command's Backend over the issue_* modules, gql bound to linear.manager_key's service."""

import contextlib
import subprocess

from agent_pm.orchestrator.engine.types import IssueError, KeychainError, LinearSettings
from agent_pm.orchestrator.trackers.linear import (
    issue_archive,
    issue_create,
    issue_edit,
    issue_label,
    issue_list,
    issue_lookup,
    issue_page,
    issue_read,
    issue_relation,
    issue_write,
)
from agent_pm.orchestrator.trackers.linear.linear_tracker import LinearTracker


class LinearIssues:
    def __init__(self, gql, linear: LinearSettings):
        self.gql, self.linear = gql, linear

    @contextlib.contextmanager
    def _keychain(self):
        try:
            yield
        except (subprocess.SubprocessError, OSError, KeychainError) as error:
            service = self.linear.manager_key
            raise IssueError(
                f"linear.manager_key Keychain service {service!r}: {type(error).__name__}"
            ) from None

    def view(self, ident, *, full):
        with self._keychain():
            return issue_read.view(self.gql, ident, full=full)

    def page(self, args, first, cursor):
        with self._keychain():
            return issue_page.fetch(self.gql, self._source(args), first, cursor)

    def walk(self, args):
        with self._keychain():
            return issue_page.walk(self.gql, self._source(args))

    def _source(self, args):
        if args.command == "list":
            return issue_list.source(self.gql, args, self.linear)
        return issue_lookup.source(self.gql, args, self.linear.team)

    def create(self, args, body):
        with self._keychain():
            return issue_create.create(
                self.gql, *issue_create.plan(self.gql, args, self.linear, body)
            )

    def edit(self, args, body):
        with self._keychain():
            return issue_edit.edit(self.gql, self.linear, args, body)

    def move(self, ident, state):
        with self._keychain():
            return issue_write.move(self.gql, self.linear, ident, state)

    def comment(self, ident, body):
        with self._keychain():
            return issue_write.comment(self.gql, ident, body)

    def relation(self, op, ident, direction, other):
        with self._keychain():
            return issue_relation.relation(self.gql, op, ident, direction, other)

    def archive_plan(self, ident):
        with self._keychain():
            return issue_archive.plan(LinearTracker(self.gql), self.gql, ident)

    def archive(self, ident):
        with self._keychain():
            return issue_archive.archive(LinearTracker(self.gql), self.gql, ident)

    def swap(self, ident, old, new):
        with self._keychain():
            return issue_label.swap(self.gql, ident, old, new)
