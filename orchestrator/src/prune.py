#!/usr/bin/env python3
"""Delete the clones of finished issues (TASK-49) and archive finished pm and engineer issues.

An issue is finished once it is Done or Canceled and its finish time (its latest
move into either, from its history; unknown means skip) is at least 24 hours ago.

Entries: for each finished issue, work/<ID>/src/* and work/<ID>/publish.
An entry must be a real directory inside its own folder (not a symlink) with a .git directory
(a core clone); anything else is skipped. A clone is deleted with any uncommitted or unpushed
work (shutil.rmtree). Remote branches and work/<ID>/ itself are never touched.

Archive: every finished issue of the team assigned to the pm or engineer role
account is archived (issueArchive, not trashed).

Runs at the end of promote's tick (Pruner); an issue is queried on its own only when it has such an entry.
"""
import os
import re
import shutil
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CLONES, WORK  # noqa: E402
from linear import parse_time  # noqa: E402
from sessions import one_line  # noqa: E402

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")
ARCHIVE_ROLES = ("pm", "engineer")
FOLDERS = (CLONES[0],)
PUBLISH = CLONES[1]

Q_ISSUE = """query($i: String!) { issue(id: $i) { state { id }
  history(first: 250, orderBy: createdAt) { nodes { createdAt toStateId } } } }"""
Q_FINISHED = """query($t: ID, $s: [ID!], $a: [ID!], $c: String) { issues(filter: { team: { id: { eq: $t } },
  state: { id: { in: $s } }, assignee: { id: { in: $a } } }, first: 50, after: $c) {
  nodes { id identifier history(first: 250, orderBy: createdAt) { nodes { createdAt toStateId } } }
  pageInfo { hasNextPage endCursor } } }"""
M_ARCHIVE = "mutation($i: String!) { issueArchive(id: $i) { success } }"


def finished_at(nodes, finished):
    """The latest move into a finished state among the history nodes, or None.
    Never the creation time: an old issue that only just finished must not look finished long ago."""
    return max((parse_time(h["createdAt"]) for h in nodes if h["toStateId"] in finished), default=None)


class Skip(Exception):
    """The entry stays; the message says why."""


class Pruner:
    def __init__(self, gql, now, dry, *, work=WORK, team, roles):
        """roles: {Linear user id: role} as linear.role_ids returns."""
        self.gql, self.now, self.dry, self.work = gql, now, dry, work
        self.team, self.roles = team, roles
        self.errors = 0

    def say(self, msg):
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def error(self, key, msg):
        self.errors += 1
        self.say(f"prune-error {key}: {msg}")

    def entries(self, ident):
        """Paths below work/<ident>/, as parts: (src, n) and (publish,)."""
        found = []
        for folder in FOLDERS:
            try:
                found += [(folder, n) for n in sorted(os.listdir(os.path.join(self.work, ident, folder)))
                          if not n.startswith(".")]
            except OSError:
                pass
        if os.path.lexists(os.path.join(self.work, ident, PUBLISH)):
            found.append((PUBLISH,))
        return found

    def prune(self, ident, parts):
        entry = os.path.join(self.work, ident, *parts)
        path = os.path.join(os.path.realpath(self.work), ident, *parts)
        key = "/".join((ident, *parts))
        try:
            if os.path.islink(entry) or not os.path.isdir(entry) or os.path.realpath(entry) != path:
                raise Skip(f"not a real directory inside {'/'.join((ident, *parts[:-1]))}/, refusing to touch")
            dotgit = os.path.join(path, ".git")
            if not os.path.isdir(dotgit) or os.path.islink(dotgit):
                raise Skip("not a clone: .git is not a directory")
            self.delete_clone(key, path)
        except Skip as e:
            self.say(f"prune-skip {key}: {e}")

    def delete_clone(self, key, path):
        if self.dry:
            self.say(f"prune-plan {key}: delete the clone")
            return
        try:
            shutil.rmtree(path)
        except OSError as e:
            self.error(key, f"rmtree: {one_line(e)}")
            return
        self.say(f"prune-removed {key}: clone")

    def archive(self, finished):
        try:
            ids = [uid for uid, role in self.roles.items() if role in ARCHIVE_ROLES]
            if not ids:
                return
            # Every page before any archive: archiving shifts the pages.
            nodes, cursor = [], None
            while True:
                page = self.gql(Q_FINISHED, t=self.team.id, s=sorted(finished), a=ids, c=cursor)["issues"]
                nodes += page["nodes"]
                end = page["pageInfo"]["endCursor"]
                if not page["pageInfo"]["hasNextPage"] or not end or end == cursor:
                    break
                cursor = end
        except (Exception, SystemExit) as e:
            self.error("archive", f"Linear: {e}")
            return
        for node in nodes:
            ident = node["identifier"]
            since = finished_at(node["history"]["nodes"], finished)
            if since is None:
                self.say(f"prune-skip {ident}: archive: finish time unknown")
            elif self.now - since < QUARANTINE:
                continue
            elif self.dry:
                self.say(f"prune-plan {ident}: archive")
            else:
                try:
                    ok = self.gql(M_ARCHIVE, i=node["id"])["issueArchive"]["success"]
                except (Exception, SystemExit) as e:
                    self.error(ident, f"archive: {e}")
                    continue
                if ok:
                    self.say(f"prune-archived {ident}")
                else:
                    self.error(ident, "archive: success: false")

    def run(self):
        """0, or 3 if anything failed."""
        finished = {self.team.states["done"], self.team.states["canceled"]}
        try:
            idents = [n for n in sorted(os.listdir(self.work))
                      if IDENT_RE.fullmatch(n) and any(os.path.isdir(os.path.join(self.work, n, *p)) for p in self.entries(n))]
        except OSError:
            idents = []
        for ident in idents:
            try:
                issue = self.gql(Q_ISSUE, i=ident)["issue"]
            except (Exception, SystemExit) as e:  # linear_gql raises SystemExit on API errors
                self.error(ident, f"Linear: {e}")
                continue
            if not issue or issue["state"]["id"] not in finished:
                continue
            since = finished_at(issue["history"]["nodes"], finished)
            if since is None:
                self.say(f"prune-skip {ident}: finish time unknown")
            elif self.now - since >= QUARANTINE:
                for parts in self.entries(ident):
                    self.prune(ident, parts)
        self.archive(finished)
        return 3 if self.errors else 0
