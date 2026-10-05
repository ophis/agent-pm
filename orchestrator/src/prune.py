#!/usr/bin/env python3
"""Delete the clones and worktrees of finished issues (TASK-49), close their TUI sessions and archive finished pm and engineer issues.

An issue is finished once it is Done or Canceled and its finish time (its latest
move into either, from its history; unknown means skip) is at least 24 hours ago.

Entries: for each finished issue, work/<ID>/src/<owner>/<name> (a legacy clone work/<ID>/src/<name> too) and
work/<ID>/publish. An entry must be a real directory inside its own folder (not a symlink) with a .git directory
(a core clone) or a .git file (a core worktree); anything else is skipped. A clone is deleted with any uncommitted or
unpushed work (shutil.rmtree). A worktree is removed by repo.remove, with its branch when pushed; one with changes or
untracked files is kept and skipped again every tick until someone cleans it by hand. A src/<owner>/ left empty is
removed. Remote branches and work/<ID>/ itself are never touched.

TUI sessions: attended.close ends a finished issue's recorded ones (logs/tui/<ID>) before its clones go, since a
left-open claude may work in one.

Archive: every finished issue of the team assigned to the pm or engineer role
account is archived (issueArchive, not trashed).

Runs at the end of promote's tick (Pruner); an issue is queried on its own only when it has such an entry or a record.
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CLONES, LOGS, WORK  # noqa: E402
import attended  # noqa: E402
import repo  # noqa: E402
from linear import HISTORY, ISSUE_ID, call, last_move, one_line, stamp  # noqa: E402

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(ISSUE_ID)
ARCHIVE_ROLES = ("pm", "engineer")
FOLDERS = (CLONES[0],)
PUBLISH = CLONES[1]

Q_ISSUE = "query($i: String!) { issue(id: $i) { state { id } " + HISTORY + " } }"
Q_FINISHED = """query($t: ID, $s: [ID!], $a: [ID!], $c: String) { issues(filter: { team: { id: { eq: $t } },
  state: { id: { in: $s } }, assignee: { id: { in: $a } } }, first: 50, after: $c) {
  nodes { id identifier """ + HISTORY + """ }
  pageInfo { hasNextPage endCursor } } }"""
M_ARCHIVE = "mutation($i: String!) { issueArchive(id: $i) { success } }"


class Skip(Exception):
    """The entry stays; the message says why."""


class Pruner:
    def __init__(self, gql, now, dry, *, work=WORK, team, roles, logs=LOGS, proc=subprocess.run, remove=repo.remove):
        """roles: {Linear user id: role} as linear.role_ids returns."""
        self.gql, self.now, self.dry, self.work = gql, now, dry, work
        self.team, self.roles, self.logs, self.proc, self.remove = team, roles, logs, proc, remove
        self.errors = 0

    def say(self, msg):
        print(f"{stamp()} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def error(self, key, msg):
        self.errors += 1
        self.say(f"prune-error {key}: {msg}")

    def entries(self, ident):
        """Paths below work/<ident>/, as parts: (src, n) legacy, (src, owner, n) and (publish,)."""
        found = []
        for folder in FOLDERS:
            base = os.path.join(self.work, ident, folder)
            try:
                names = [n for n in sorted(os.listdir(base)) if not n.startswith(".")]
            except OSError:
                continue
            for n in names:
                entry = os.path.join(base, n)
                if os.path.islink(entry) or not os.path.isdir(entry) or os.path.lexists(os.path.join(entry, ".git")):
                    found.append((folder, n))
                    continue
                try:
                    found += [(folder, n, m) for m in sorted(os.listdir(entry)) if not m.startswith(".")]
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
            if os.path.islink(dotgit):
                raise Skip("not a clone or worktree")
            if os.path.isdir(dotgit):
                self.delete_clone(key, path)
            elif os.path.isfile(dotgit):
                self.remove_worktree(key, path, ident)
            else:
                raise Skip("not a clone or worktree")
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

    def remove_worktree(self, key, path, ident):
        if self.dry:
            self.say(f"prune-plan {key}: remove the worktree")
            return
        try:
            res = self.remove(path, f"{ident}-")
        except repo.Invalid as e:
            raise Skip(one_line(str(e)))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.error(key, one_line(e))
            return
        branch, kept = res["branch"], res["kept"]
        if not branch:
            self.say(f"prune-removed {key}: worktree")
        elif kept:
            self.say(f"prune-removed {key}: worktree; branch {branch} kept: {kept}")
        else:
            self.say(f"prune-removed {key}: worktree and branch {branch}")

    def rmdir_owners(self, ident, owners):
        for owner in sorted(owners):
            try:
                os.rmdir(os.path.join(self.work, ident, *owner))
            except OSError:
                pass

    def close_sessions(self, ident):
        if self.dry:
            try:
                names = attended.names(ident, logs=self.logs)
            except OSError as e:
                self.error(ident, one_line(e))
                return
            for name in names:
                self.say(f"prune-plan {ident}/{name}: close the tui session")
            return
        for c in attended.close(ident, logs=self.logs, proc=self.proc):
            key = ident if c.name is None else f"{ident}/{c.name}"
            if c.status == "closed":
                self.say(f"prune-closed {key}: tui session")
            elif c.status == "skip":
                self.say(f"prune-skip {key}: {c.msg}")
            else:
                self.error(key, c.msg)

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
            since = last_move(node["history"]["nodes"], finished)
            if since is None:
                self.say(f"prune-skip {ident}: archive: finish time unknown")
            elif self.now - since < QUARANTINE:
                continue
            elif self.dry:
                self.say(f"prune-plan {ident}: archive")
            else:
                try:
                    call(self.gql, M_ARCHIVE, "issueArchive", i=node["id"])
                except (Exception, SystemExit) as e:
                    self.error(ident, f"archive: {e}")
                    continue
                self.say(f"prune-archived {ident}")

    def run(self):
        """0, or 3 if anything failed."""
        finished = {self.team.states["done"], self.team.states["canceled"]}
        try:
            idents = [n for n in sorted(os.listdir(self.work))
                      if IDENT_RE.fullmatch(n) and any(os.path.isdir(os.path.join(self.work, n, *p)) for p in self.entries(n))]
        except OSError:
            idents = []
        idents = sorted(set(idents) | set(attended.recorded(self.logs)))
        for ident in idents:
            try:
                issue = self.gql(Q_ISSUE, i=ident)["issue"]
            except (Exception, SystemExit) as e:  # linear_gql raises SystemExit on API errors
                self.error(ident, f"Linear: {e}")
                continue
            if not issue or issue["state"]["id"] not in finished:
                continue
            since = last_move(issue["history"]["nodes"], finished)
            if since is None:
                self.say(f"prune-skip {ident}: finish time unknown")
            elif self.now - since >= QUARANTINE:
                self.close_sessions(ident)
                entries = self.entries(ident)
                for parts in entries:
                    self.prune(ident, parts)
                if not self.dry:
                    self.rmdir_owners(ident, {parts[:-1] for parts in entries if len(parts) == 3})
        self.archive(finished)
        return 3 if self.errors else 0
