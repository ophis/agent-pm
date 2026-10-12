#!/usr/bin/env python3
"""Prune, for each issue Done or Canceled at least 24 hours ago (its latest move into either; unknown: skipped):
- closes its live TUI sessions (attended.close) before its checkouts go, since a left-open claude may work in one;
- deletes <work_dir>/work/<ID>/src/<owner>/*, publish and tmp, each a real directory inside its own folder, a src or
  publish entry only as a clone or worktree (.git a directory or a file), uncommitted work included;
- in a deleted worktree's local clone, unless it lies in <work_dir>/work or a temp dir: `worktree prune`, then deletes
  each <ID>-* branch but the default that origin/<branch> or origin/<default> holds, else keeps and reports it (git
  reads refs only, so code an agent run planted, such as filters or hooks, never runs outside auto mode's review);
- archives every finished issue of the team assigned to the pm or engineer role account.
Each step is logged (linear.log, src prune); a failure is logged and the rest goes on (run returns 3), but
linear.Unavailable is raised.
"""

import functools
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
import tmux_host  # noqa: E402
from linear import HISTORY, ISSUE_ID, Unavailable, call, last_move, log, one_line  # noqa: E402

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(ISSUE_ID)
ARCHIVE_ROLES = ("pm", "engineer")
FOLDERS = (CLONES[0],)
PUBLISH = CLONES[1]
TMP = "tmp"  # core/team/principles.md: an agent run's temp files

Q_ISSUE = "query($i: String!) { issue(id: $i) { state { id } " + HISTORY + " } }"
Q_FINISHED = (
    """query($t: ID, $s: [ID!], $a: [ID!], $c: String) { issues(filter: { team: { id: { eq: $t } },
  state: { id: { in: $s } }, assignee: { id: { in: $a } } }, first: 50, after: $c) {
  nodes { id identifier """
    + HISTORY
    + """ }
  pageInfo { hasNextPage endCursor } } }"""
)
M_ARCHIVE = "mutation($i: String!) { issueArchive(id: $i) { success } }"


class Skip(Exception):
    pass


class Pruner:
    def __init__(
        self,
        gql,
        now,
        dry,
        *,
        work=RUNS_DIR,
        team,
        roles,
        proc=subprocess.run,
        run=repo.sh,
        writable=None,
    ):
        self.gql, self.now, self.dry, self.work = gql, now, dry, work
        self.team, self.roles, self.proc, self.run_git = team, roles, proc, run
        self.writable = config.writable(work) if writable is None else writable
        self.errors = 0
        self.say = functools.partial(log, "prune", dry=dry)

    def error(self, issue=None, **fields):
        self.errors += 1
        self.say("prune-error", issue, **fields)

    def entries(self, ident):
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
                    found += [
                        (folder, n, m)
                        for m in sorted(os.listdir(entry))
                        if not m.startswith(".") or os.path.isdir(os.path.join(entry, m))
                    ]
                except OSError:
                    pass
        found += [
            (d,) for d in (PUBLISH, TMP) if os.path.lexists(os.path.join(self.work, ident, d))
        ]
        return found

    def prune(self, ident, parts):
        entry = os.path.join(self.work, ident, *parts)
        path = os.path.join(os.path.realpath(self.work), ident, *parts)
        key = "/".join((ident, *parts))
        try:
            if os.path.islink(entry) or not os.path.isdir(entry) or os.path.realpath(entry) != path:
                raise Skip(
                    f"not a real directory inside {'/'.join((ident, *parts[:-1]))}/, refusing to touch"
                )
            if parts == (TMP,):
                self.delete(key, path, "temp files")
                return None
            dotgit = os.path.join(path, ".git")
            if os.path.islink(dotgit) or not (os.path.isdir(dotgit) or os.path.isfile(dotgit)):
                raise Skip("not a clone or worktree")
            common = repo.common_dir(path) if os.path.isfile(dotgit) else None
            return (
                common
                if self.delete(key, path, "worktree" if os.path.isfile(dotgit) else "clone")
                else None
            )
        except Skip as e:
            self.say("prune-skip", once=True, entry=key, reason=str(e))

    def delete(self, key, path, what):
        if self.dry:
            self.say("prune-plan", entry=key, what=f"delete the {what}")
            return True
        try:
            shutil.rmtree(path)
        except OSError as e:
            self.error(entry=key, error=f"rmtree: {one_line(e)}")
            return False
        self.say("prune-removed", entry=key, what=what)
        return True

    def clean_clone(self, ident, common):
        clone = os.path.dirname(common)
        if root := repo.under(common, self.writable):
            self.say(
                "prune-skip",
                ident,
                once=True,
                reason=f"{clone} is under {root}, so no git runs there",
            )
            return
        if self.dry:
            self.say(
                "prune-plan",
                ident,
                what=f"prune {clone}'s worktree records and pushed {ident}-* branches",
            )
            return
        at = ["git", *repo.GUARD, "-C", clone, f"--git-dir={common}"]
        try:
            repo.git(self.run_git, clone, at[-1], "worktree", "prune", pre=repo.GUARD)
            head = self.run_git(
                [*at, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], repo.SHORT
            )
            default = head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else None
            listed = repo.git(
                self.run_git,
                clone,
                at[-1],
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/{ident}-*",
                pre=repo.GUARD,
            )
            for branch in listed.split():
                if branch == default:
                    continue
                if any(
                    self.run_git(
                        [
                            *at,
                            "merge-base",
                            "--is-ancestor",
                            f"refs/heads/{branch}",
                            f"refs/remotes/origin/{b}",
                        ],
                        repo.SHORT,
                    ).returncode
                    == 0
                    for b in dict.fromkeys(filter(None, (branch, default)))
                ):
                    repo.git(self.run_git, clone, at[-1], "branch", "-D", branch, pre=repo.GUARD)
                    self.say("prune-removed", ident, what=f"branch {branch} in {clone}")
                else:
                    self.say(
                        "prune-skip",
                        ident,
                        once=True,
                        reason=f"branch {branch} in {clone} kept: not pushed",
                    )
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.error(ident, error=one_line(e))

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
            except tmux_host.TuiError as e:
                self.error(ident, error=one_line(e))
                return
            for name in names:
                self.say("prune-plan", ident, session=name, what="close the tui session")
            return
        for c in attended.close(ident, proc=self.proc):
            if c.status == "closed":
                self.say("prune-closed", ident, session=c.name)
            else:
                self.error(ident, session=c.name, error=c.msg)

    def archive(self, finished):
        try:
            ids = [uid for uid, role in self.roles.items() if role in ARCHIVE_ROLES]
            if not ids:
                return
            # Every page before any archive: archiving shifts the pages.
            nodes, cursor = [], None
            while True:
                page = self.gql(Q_FINISHED, t=self.team.id, s=sorted(finished), a=ids, c=cursor)[
                    "issues"
                ]
                nodes += page["nodes"]
                end = page["pageInfo"]["endCursor"]
                if not page["pageInfo"]["hasNextPage"] or not end or end == cursor:
                    break
                cursor = end
        except Unavailable:
            raise
        except (Exception, SystemExit) as e:
            self.error(error=f"archive: Linear: {one_line(e)}")
            return
        for node in nodes:
            ident = node["identifier"]
            since = last_move(node["history"]["nodes"], finished)
            if since is None:
                self.say("prune-skip", ident, once=True, reason="archive: finish time unknown")
            elif self.now - since < QUARANTINE:
                continue
            elif self.dry:
                self.say("prune-plan", ident, what="archive")
            else:
                try:
                    call(self.gql, M_ARCHIVE, "issueArchive", i=node["id"])
                except Unavailable:
                    raise
                except (Exception, SystemExit) as e:
                    self.error(ident, error=f"archive: {one_line(e)}")
                    continue
                self.say("prune-archived", ident)

    def run(self):
        finished = {self.team.states["done"], self.team.states["canceled"]}
        try:
            idents = [
                n
                for n in sorted(os.listdir(self.work))
                if IDENT_RE.fullmatch(n)
                and any(os.path.isdir(os.path.join(self.work, n, *p)) for p in self.entries(n))
            ]
        except OSError:
            idents = []
        try:
            live = attended.issues(proc=self.proc)
        except tmux_host.TuiError as e:
            self.error(error=f"tui: {one_line(e)}")
            live = []
        idents = sorted(set(idents) | set(live))
        for ident in idents:
            try:
                issue = self.gql(Q_ISSUE, i=ident)["issue"]
            except Unavailable:
                raise
            except (Exception, SystemExit) as e:
                self.error(ident, error=f"Linear: {one_line(e)}")
                continue
            if not issue or issue["state"]["id"] not in finished:
                continue
            since = last_move(issue["history"]["nodes"], finished)
            if since is None:
                self.say("prune-skip", ident, once=True, reason="finish time unknown")
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
