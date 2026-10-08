#!/usr/bin/env python3
"""Delete finished issues' checkouts (TASK-49), close their TUI sessions and archive finished pm and engineer issues.

An issue is finished once it is Done or Canceled and its finish time (its latest
move into either, from its history; unknown means skip) is at least 24 hours ago.

Entries: for each finished issue, <work_dir>/work/<ID>/src/<owner>/*, <work_dir>/work/<ID>/publish and
<work_dir>/work/<ID>/tmp, each a real directory inside its own folder; a src or publish entry must be a clone
or a worktree (.git a directory or a file), else it is skipped. Each goes with shutil.rmtree, uncommitted work included;
the rest of <work_dir>/work/<ID>/ and <work_dir>/logs stay. A worktree's .git file is read first (repo.common_dir, no git) for its
local clone; there, unless the clone lies in <work_dir>/work or a temp dir, git reads refs only: `worktree prune`, then each
<ID>-* branch but the default is deleted when origin/<branch> or origin/<default> holds it, else kept and reported. Remote
branches are never touched. Agent runs and the harness are the same macOS user: this keeps code an agent run planted
(filters, hooks, submodules) from running outside auto mode's review; it is no privilege boundary.

TUI sessions: attended.close ends a finished issue's live ones before its checkouts go, since a left-open claude may
work in one.

Archive: every finished issue of the team assigned to the pm or engineer role
account is archived (issueArchive, not trashed).

An issue is queried on its own only when it has such an entry or a live TUI session.
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
from config import CLONES, RUNS_DIR  # noqa: E402
import attended  # noqa: E402
import repo  # noqa: E402
import tui_claude  # noqa: E402
from linear import HISTORY, ISSUE_ID, call, last_move, one_line, stamp  # noqa: E402

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(ISSUE_ID)
ARCHIVE_ROLES = ("pm", "engineer")
FOLDERS = (CLONES[0],)
PUBLISH = CLONES[1]
TMP = "tmp"   # core/team/principles.md: an agent run's temp files

Q_ISSUE = "query($i: String!) { issue(id: $i) { state { id } " + HISTORY + " } }"
Q_FINISHED = """query($t: ID, $s: [ID!], $a: [ID!], $c: String) { issues(filter: { team: { id: { eq: $t } },
  state: { id: { in: $s } }, assignee: { id: { in: $a } } }, first: 50, after: $c) {
  nodes { id identifier """ + HISTORY + """ }
  pageInfo { hasNextPage endCursor } } }"""
M_ARCHIVE = "mutation($i: String!) { issueArchive(id: $i) { success } }"


class Skip(Exception):
    """The entry stays; the message says why."""


class Pruner:
    def __init__(self, gql, now, dry, *, work=RUNS_DIR, team, roles, proc=subprocess.run, run=repo.sh, writable=None):
        """roles: {Linear user id: role} as linear.role_ids returns. run: git's runner. writable: dirs whose clones get
        no git run (default <work_dir>/work and the temp dirs)."""
        self.gql, self.now, self.dry, self.work = gql, now, dry, work
        self.team, self.roles, self.proc, self.run_git = team, roles, proc, run
        self.writable = config.writable(work) if writable is None else writable
        self.errors = 0

    def say(self, msg):
        print(f"{stamp()} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def error(self, key, msg):
        self.errors += 1
        self.say(f"prune-error {key}: {msg}")

    def entries(self, ident):
        """Paths below <work_dir>/work/<ident>/, as parts: (src, owner, n), (src, owner) when not a real directory (prune
        skips it), (publish,) and (tmp,)."""
        found = []
        for folder in FOLDERS:
            base = os.path.join(self.work, ident, folder)
            try:
                names = [n for n in sorted(os.listdir(base)) if not n.startswith(".")]
            except OSError:
                continue
            for n in names:
                entry = os.path.join(base, n)
                if os.path.islink(entry) or not os.path.isdir(entry):
                    found.append((folder, n))
                    continue
                try:
                    found += [(folder, n, m) for m in sorted(os.listdir(entry))
                              if not m.startswith(".") or os.path.isdir(os.path.join(entry, m))]
                except OSError:
                    pass
        found += [(d,) for d in (PUBLISH, TMP) if os.path.lexists(os.path.join(self.work, ident, d))]
        return found

    def prune(self, ident, parts):
        """Deletes one entry; for a worktree, the clone's git dir its `.git` file names (read before the delete)."""
        entry = os.path.join(self.work, ident, *parts)
        path = os.path.join(os.path.realpath(self.work), ident, *parts)
        key = "/".join((ident, *parts))
        try:
            if os.path.islink(entry) or not os.path.isdir(entry) or os.path.realpath(entry) != path:
                raise Skip(f"not a real directory inside {'/'.join((ident, *parts[:-1]))}/, refusing to touch")
            if parts == (TMP,):
                self.delete(key, path, "temp files")
                return None
            dotgit = os.path.join(path, ".git")
            if os.path.islink(dotgit) or not (os.path.isdir(dotgit) or os.path.isfile(dotgit)):
                raise Skip("not a clone or worktree")
            common = repo.common_dir(path) if os.path.isfile(dotgit) else None
            return common if self.delete(key, path, "worktree" if os.path.isfile(dotgit) else "clone") else None
        except Skip as e:
            self.say(f"prune-skip {key}: {e}")

    def delete(self, key, path, what):
        """True once `path` is deleted (planned, in a dry run)."""
        if self.dry:
            self.say(f"prune-plan {key}: delete the {what}")
            return True
        try:
            shutil.rmtree(path)
        except OSError as e:
            self.error(key, f"rmtree: {one_line(e)}")
            return False
        self.say(f"prune-removed {key}: {what}")
        return True

    def clean_clone(self, ident, common):
        """In the local clone of deleted worktrees: drops their records and the <ident>-* branches origin holds."""
        clone = os.path.dirname(common)
        if root := repo.under(common, self.writable):
            self.say(f"prune-skip {ident}: {clone} is under {root}, so no git runs there")
            return
        if self.dry:
            self.say(f"prune-plan {ident}: prune {clone}'s worktree records and pushed {ident}-* branches")
            return
        at = ["git", *repo.GUARD, "-C", clone, f"--git-dir={common}"]
        try:
            repo.git(self.run_git, clone, at[-1], "worktree", "prune", pre=repo.GUARD)
            head = self.run_git([*at, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], repo.SHORT)
            default = head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else None
            listed = repo.git(self.run_git, clone, at[-1], "for-each-ref", "--format=%(refname:short)",
                              f"refs/heads/{ident}-*", pre=repo.GUARD)
            for branch in listed.split():
                if branch == default:
                    continue
                if any(self.run_git([*at, "merge-base", "--is-ancestor", f"refs/heads/{branch}", f"refs/remotes/origin/{b}"],
                                    repo.SHORT).returncode == 0 for b in dict.fromkeys(filter(None, (branch, default)))):
                    repo.git(self.run_git, clone, at[-1], "branch", "-D", branch, pre=repo.GUARD)
                    self.say(f"prune-removed {ident}: branch {branch} in {clone}")
                else:
                    self.say(f"prune-skip {ident}: branch {branch} in {clone} kept: not pushed")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.error(ident, one_line(e))

    def rmdir_owners(self, ident, owners):
        for owner in sorted(owners):
            try:
                os.rmdir(os.path.join(self.work, ident, *owner))
            except OSError:
                pass

    def close_sessions(self, ident):
        if self.dry:
            try:
                names = attended.sessions(ident, proc=self.proc)
            except tui_claude.TuiError as e:
                self.error(ident, one_line(e))
                return
            for name in names:
                self.say(f"prune-plan {ident}/{name}: close the tui session")
            return
        for c in attended.close(ident, proc=self.proc):
            key = ident if c.name is None else f"{ident}/{c.name}"
            if c.status == "closed":
                self.say(f"prune-closed {key}: tui session")
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
        try:
            live = attended.issues(proc=self.proc)
        except tui_claude.TuiError as e:
            self.error("tui", one_line(e))
            live = []
        idents = sorted(set(idents) | set(live))
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
                commons = {c for parts in entries if (c := self.prune(ident, parts))}
                if not self.dry:
                    self.rmdir_owners(ident, {parts[:-1] for parts in entries if len(parts) == 3})
                for common in sorted(commons):
                    self.clean_clone(ident, common)
        self.archive(finished)
        return 3 if self.errors else 0
