#!/usr/bin/env python3
"""Delete the worktrees of finished issues (TASK-49) and archive finished pm and engineer issues.

An issue is finished once it is Done or Canceled and its finish time (its latest
move into either, from its history; unknown means skip) is at least 24 hours ago.

Worktrees: for each finished issue, every worktree under work/<ID>/worktrees/ is
force-deleted with any uncommitted or unpushed work: `git worktree remove --force
--force` (dirty and locked ones too), then `git branch -D` of its local branch.
An entry must be a real directory inside work/<ID>/worktrees/, and its clone is
read from its .git file (<clone>/.git/worktrees/<n>); anything else is skipped.
Remote branches, main workspaces and work/<ID>/ itself are never touched.

Archive: every finished issue of the team assigned to the pm or engineer role
account is archived (issueArchive, not trashed).

Runs at the end of promote's tick; an issue is queried on its own only when it has a worktree.
--dry-run   Print the plan; change nothing.
Needs Python 3.11+ (tomllib).
Exit 0 = done, 2 = bad arguments, 3 = an error (Linear or git), retried next run.
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eng import LONG, SHORT, TransientError, _stderr, locate, sh_run  # noqa: E402
from pipeline import WORK, linear_gql, load_config, parse_time, role_ids, runnable, team as linear_team  # noqa: E402

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")
ARCHIVE_ROLES = ("pm", "engineer")

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
    """The worktree stays; the message says why."""


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

    def git(self, clone, *args, timeout=SHORT):
        try:
            res = self.git_run(["git", "-C", clone, *args], timeout)
        except (subprocess.TimeoutExpired, OSError, ValueError) as e:
            raise TransientError(f"git {args[0]} {args[1]}: {type(e).__name__}") from None
        if res.returncode != 0:
            raise TransientError(f"git {args[0]} {args[1]}: {_stderr(res)}")

    def entries(self, ident):
        base = os.path.join(self.work, ident, "worktrees")
        try:
            return [os.path.join(base, n) for n in sorted(os.listdir(base)) if not n.startswith(".")]
        except OSError:
            return []

    def prune(self, ident, entry):
        name = os.path.basename(entry)
        key = f"{ident}/{name}"
        path = os.path.join(os.path.realpath(self.work), ident, "worktrees", name)
        try:
            if os.path.islink(entry) or not os.path.isdir(entry) or os.path.realpath(entry) != path:
                raise Skip(f"not a real directory inside {ident}/worktrees/, refusing to touch")
            try:
                clone, branch, _ = locate(path)
            except ValueError as e:
                raise Skip(e) from None
            if self.dry:
                self.say(f"prune-plan {key}: force-delete the worktree" + (f" and local branch {branch}" if branch else ""))
                return
            self.git(clone, "worktree", "remove", "--force", "--force", "--", path, timeout=LONG)
            self.say(f"prune-removed {key}: worktree")
            if branch:
                self.git(clone, "branch", "-D", "--", branch)
                self.say(f"prune-removed {key}: local branch {branch}")
        except Skip as e:
            self.say(f"prune-skip {key}: {e}")
        except TransientError as e:
            self.error(key, e)

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
                      if IDENT_RE.fullmatch(n) and any(os.path.isdir(p) for p in self.entries(n))]
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
                for entry in self.entries(ident):
                    self.prune(ident, entry)
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
