#!/usr/bin/env python3
"""Delete the worktrees of finished issues (TASK-49).

Once an issue is Done or Canceled and its finish time (from its history; unknown
means skip) is at least 24 hours ago, every worktree under work/<ID>/worktrees/ is
force-deleted with any uncommitted or unpushed work: `git worktree remove --force
--force` (dirty and locked ones too), then `git branch -D` of its local branch.
An entry must be a real directory inside work/<ID>/worktrees/, and its clone is
read from its .git file (<clone>/.git/worktrees/<n>); anything else is skipped.
Remote branches, main workspaces and work/<ID>/ itself are never touched.

Runs at the end of promote's tick; Linear is only queried when a worktree exists.
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
from eng import LONG, REF, SHORT, TransientError, _stderr, sh_run  # noqa: E402
from pipeline import WORK, linear_gql, load_config, parse_time, team as linear_team  # noqa: E402

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")

Q_ISSUE = """query($i: String!) { issue(id: $i) { state { id }
  history(first: 250, orderBy: createdAt) { nodes { createdAt toStateId } } } }"""


class Skip(Exception):
    """The worktree stays; the message says why."""


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
    """(clone, local branch or None if detached) of the worktree at path, read from files only."""
    text = _read(os.path.join(path, ".git"))
    if not text.startswith("gitdir: "):
        raise Skip("clone unknown: .git is not a gitdir file")
    gitdir = os.path.realpath(os.path.join(path, text[len("gitdir: "):]))
    parent = os.path.dirname(gitdir)
    if os.path.basename(parent) != "worktrees" or os.path.basename(os.path.dirname(parent)) != ".git":
        raise Skip("clone unknown: gitdir is not <clone>/.git/worktrees/<name>")
    head = _read(os.path.join(gitdir, "HEAD"))
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None
    if branch is not None and not REF.fullmatch(branch):
        raise Skip("unsafe branch name")
    return os.path.dirname(os.path.dirname(parent)), branch


class Pruner:
    def __init__(self, gql, cfg, now, dry, run=sh_run, work=WORK, team=None):
        self.gql, self.cfg, self.now, self.dry, self.git_run, self.work, self.team = gql, cfg, now, dry, run, work, team
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
            clone, branch = locate(path)
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

    def run(self):
        """0, or 3 if anything failed."""
        try:
            idents = [n for n in sorted(os.listdir(self.work))
                      if IDENT_RE.fullmatch(n) and any(os.path.isdir(p) for p in self.entries(n))]
        except OSError:
            return 0
        if not idents:  # the common case: no Linear query
            return 0
        states = (self.team or linear_team(self.gql, self.cfg)).states
        finished = {states["done"], states["canceled"]}
        for ident in idents:
            try:
                issue = self.gql(Q_ISSUE, i=ident)["issue"]
            except (Exception, SystemExit) as e:  # linear_gql raises SystemExit on API errors
                self.error(ident, f"Linear: {e}")
                continue
            if not issue or issue["state"]["id"] not in finished:
                continue
            # Never the creation time: an old issue that only just finished must not look finished long ago.
            since = max((parse_time(h["createdAt"]) for h in issue["history"]["nodes"]
                         if h["toStateId"] in finished), default=None)
            if since is None:
                self.say(f"prune-skip {ident}: finish time unknown")
            elif self.now - since >= QUARANTINE:
                for entry in self.entries(ident):
                    self.prune(ident, entry)
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
