#!/usr/bin/env python3
"""Delete the clones and worktrees of finished issues (TASK-49) and archive finished pm and engineer issues.

An issue is finished once it is Done or Canceled and its finish time (its latest
move into either, from its history; unknown means skip) is at least 24 hours ago.

Entries: for each finished issue, work/<ID>/worktrees/*, work/<ID>/src/* and work/<ID>/publish.
An entry must be a real directory inside its own folder (not a symlink); anything else is skipped.
Then, by its .git:
- a directory (a core clone, left in src/<name> and publish/): deleted with any uncommitted or
  unpushed work (shutil.rmtree);
- a file (a linked worktree from before core, or a researcher's detached, read-only checkout):
  force-deleted with any such work: `git worktree remove --force --force` (dirty and locked ones
  too), then `git branch -D` of its local branch, if it has one. Its clone is read from the .git
  file (<clone>/.git/worktrees/<n>); anything else is skipped.
Remote branches and work/<ID>/ itself are never touched.

Archive: every finished issue of the team assigned to the pm or engineer role
account is archived (issueArchive, not trashed).

Runs at the end of promote's tick; an issue is queried on its own only when it has such an entry.
--dry-run   Print the plan; change nothing.
Needs Python 3.11+ (tomllib).
Exit 0 = done, 2 = bad arguments, 3 = an error (Linear or git), retried next run.
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import (CLONES, LONG, REF, SHORT, WORK, err_text, linear_gql, load_config, parse_time,  # noqa: E402
                      role_ids, runnable, sh_run, team as linear_team)
from sessions import one_line  # noqa: E402

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")
ARCHIVE_ROLES = ("pm", "engineer")
FOLDERS = ("worktrees", CLONES[0])
PUBLISH = CLONES[1]

Q_ISSUE = """query($i: String!) { issue(id: $i) { state { id }
  history(first: 250, orderBy: createdAt) { nodes { createdAt toStateId } } } }"""
Q_FINISHED = """query($t: ID, $s: [ID!], $a: [ID!], $c: String) { issues(filter: { team: { id: { eq: $t } },
  state: { id: { in: $s } }, assignee: { id: { in: $a } } }, first: 50, after: $c) {
  nodes { id identifier history(first: 250, orderBy: createdAt) { nodes { createdAt toStateId } } }
  pageInfo { hasNextPage endCursor } } }"""
M_ARCHIVE = "mutation($i: String!) { issueArchive(id: $i) { success } }"


class TransientError(Exception):
    """A git call failed or returned something unusable."""


def finished_at(nodes, finished):
    """The latest move into a finished state among the history nodes, or None.
    Never the creation time: an old issue that only just finished must not look finished long ago."""
    return max((parse_time(h["createdAt"]) for h in nodes if h["toStateId"] in finished), default=None)


def _read(path):
    """A regular file's stripped text, or "" (never blocks on a FIFO)."""
    try:
        if os.path.isfile(path):
            with open(path) as f:
                return f.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def locate(path):
    """(clone, local branch or None if detached, HEAD text, gitdir) of the worktree at path, read from files only;
    ValueError if it is not a linked worktree."""
    text = _read(os.path.join(path, ".git"))
    if not text.startswith("gitdir: "):
        raise ValueError("clone unknown: .git is not a gitdir file")
    gitdir = os.path.realpath(os.path.join(path, text[len("gitdir: "):]))
    parent = os.path.dirname(gitdir)
    if os.path.basename(parent) != "worktrees" or os.path.basename(os.path.dirname(parent)) != ".git":
        raise ValueError("clone unknown: gitdir is not <clone>/.git/worktrees/<name>")
    head = _read(os.path.join(gitdir, "HEAD"))
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None
    if branch is not None and not REF.fullmatch(branch):
        raise ValueError("unsafe branch name")
    return os.path.dirname(os.path.dirname(parent)), branch, head, gitdir


def _git(run, clone, *args, timeout=SHORT):
    # A run can plant config in the clone; these keep git from running its code as the harness.
    try:
        res = run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", clone, *args], timeout)
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        raise TransientError(f"git {args[0]} {args[1]}: {type(e).__name__}") from None
    if res.returncode != 0:
        raise TransientError(f"git {args[0]} {args[1]}: {err_text(res)}")


def remove_linked(path, *, run=sh_run):
    """Force-remove the linked worktree at path (dirty or locked too), then its local branch.
    Returns the branch or None; ValueError if path is not a linked worktree, TransientError if git fails."""
    clone, branch, _, _ = locate(path)
    _git(run, clone, "worktree", "remove", "--force", "--force", "--", path, timeout=LONG)
    if branch:
        _git(run, clone, "branch", "-D", "--", branch)
    return branch


class Skip(Exception):
    """The entry stays; the message says why."""


class Pruner:
    def __init__(self, gql, cfg, now, dry, run=sh_run, work=WORK, team=None, roles=None):
        """roles: {Linear user id: role} as pipeline.role_ids returns; None resolves it in the archive step."""
        self.gql, self.cfg, self.now, self.dry, self.git_run, self.work = gql, cfg, now, dry, run, work
        self.team, self.roles = team, roles
        self.errors = 0

    def say(self, msg):
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def error(self, key, msg):
        self.errors += 1
        self.say(f"prune-error {key}: {msg}")

    def entries(self, ident):
        """Paths below work/<ident>/, as parts: (worktrees, n), (src, n) and (publish,)."""
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
        key = f"{ident}/{parts[-1]}" if parts[0] == "worktrees" else "/".join((ident, *parts))
        try:
            if os.path.islink(entry) or not os.path.isdir(entry) or os.path.realpath(entry) != path:
                raise Skip(f"not a real directory inside {'/'.join((ident, *parts[:-1]))}/, refusing to touch")
            dotgit = os.path.join(path, ".git")
            if os.path.isdir(dotgit) and not os.path.islink(dotgit):
                self.delete_clone(key, path)
            else:
                self.remove_worktree(key, path)
        except Skip as e:
            self.say(f"prune-skip {key}: {e}")
        except TransientError as e:
            self.error(key, e)

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

    def remove_worktree(self, key, path):
        try:
            branch = locate(path)[1] if self.dry else remove_linked(path, run=self.git_run)
        except ValueError as e:
            raise Skip(e) from None
        if self.dry:
            self.say(f"prune-plan {key}: force-delete the worktree" + (f" and local branch {branch}" if branch else ""))
            return
        self.say(f"prune-removed {key}: worktree")
        if branch:
            self.say(f"prune-removed {key}: local branch {branch}")

    def archive(self, team, finished):
        try:
            roles = role_ids(self.gql, runnable(self.cfg)) if self.roles is None else self.roles
            ids = [uid for uid, role in roles.items() if role in ARCHIVE_ROLES]
            if not ids:
                return
            # Every page before any archive: archiving shifts the pages.
            nodes, cursor = [], None
            while True:
                page = self.gql(Q_FINISHED, t=team.id, s=sorted(finished), a=ids, c=cursor)["issues"]
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
        team = self.team or linear_team(self.gql, self.cfg)
        finished = {team.states["done"], team.states["canceled"]}
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
        self.archive(team, finished)
        return 3 if self.errors else 0


def main(argv, gql=linear_gql, run=sh_run, work=WORK, now=None):
    import argparse
    ap = argparse.ArgumentParser(prog="prune.py")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        return Pruner(gql, load_config(), now or datetime.now().astimezone(), a.dry_run, run=run, work=work).run()
    except SystemExit as e:
        if not str(e).startswith("linear api error"):
            raise
        print(f"prune.py: transient: {e}", file=sys.stderr)
        return 3
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"prune.py: transient: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
