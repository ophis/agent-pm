#!/usr/bin/env python3
"""Prune worktrees of finished issues (TASK-49).

For each issue Done or Canceled for at least 24 hours (by its history; unknown
means skip), removes each worktree under work/<ID>/worktrees/ with `git worktree
remove` (never --force) and deletes its local branch, but only if the worktree is
registered with a clone outside work/, has no uncommitted, untracked or ignored
files (regenerable CACHES aside), and its commits are on its upstream branch on
the remote. Anything else is skipped and logged. Remote branches, main
workspaces and work/<ID>/ itself are never touched.

Runs at the end of promote's tick (every 15 minutes); a prune failure is logged
and never breaks promote. Linear is only queried when a worktree exists on
disk, so an idle tick costs nothing.

--dry-run   Print the plan; change nothing (not even a fetch).
Needs Python 3.11+ (tomllib).
Exit 0 = done (skips are normal), 2 = bad arguments, 3 = a transient failure
(Linear, git or the network); it is retried on the next run.
"""
import fnmatch
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eng import REF, SHORT, TransientError, _stderr, sh_run  # noqa: E402
from pipeline import WORK, linear_gql, load_config, parse_time  # noqa: E402

QUARANTINE = timedelta(hours=24)
FINISHED = ("Done", "Canceled")
IDENT_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")
# The only ignored files `worktree remove` may delete; any other (.env, notes) keeps the worktree.
CACHES = ("__pycache__", "*.pyc", ".pytest_cache", "node_modules", ".venv", ".DS_Store")
# Config may name programs git runs (fsmonitor, hooks); prune runs none of them.
SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")

Q_SETUP = """query($t: String!) {
  workflowStates(filter: { team: { name: { eq: $t } } }) { nodes { id name } } }"""
Q_ISSUE = """query($i: String!) { issue(id: $i) { identifier state { name } createdAt
  history(first: 250, orderBy: createdAt) { nodes { createdAt toStateId } } } }"""


class Skip(Exception):
    """The worktree stays; the message says why."""


class Plan(NamedTuple):
    path: str  # the worktree's real path, as checked
    clone: str
    branch: str


def regenerable(path):
    return any(fnmatch.fnmatchcase(part, c) for part in path.rstrip("/").split("/") for c in CACHES)


def _read(path):
    """A small regular file's stripped text, or "" (never blocks on a FIFO or device)."""
    try:
        if os.path.isfile(path):
            with open(path) as f:
                return f.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


class Pruner:
    def __init__(self, gql, cfg, now, dry, run=sh_run, work=WORK):
        self.gql, self.cfg, self.now, self.dry = gql, cfg, now, dry
        self.git_run, self.work = run, work

    def setup(self):
        nodes = self.gql(Q_SETUP, t=self.cfg["team"])["workflowStates"]["nodes"]
        states = {s["name"]: s["id"] for s in nodes}
        missing = [s for s in FINISHED if s not in states]
        if missing:
            raise SystemExit(f"not found in Linear: {', '.join(missing)}")
        self.finished_ids = {states[s] for s in FINISHED}

    def say(self, msg):
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def _run(self, cwd, *args):
        """The git result; TransientError if git cannot run at all."""
        try:
            return self.git_run(["git", *SAFE, "-C", cwd, *args], SHORT)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise TransientError(f"git -C {cwd} {' '.join(args)}"[:120] + f": {type(e).__name__}") from None

    def _git(self, cwd, *args):
        """stdout; TransientError unless git exits 0."""
        res = self._run(cwd, *args)
        if res.returncode != 0:
            raise TransientError(f"git -C {cwd} {' '.join(args)}"[:120] + f": {_stderr(res)}")
        return res.stdout

    def finished_since(self, detail):
        """When the issue last entered Done/Canceled, or None if the history doesn't say.

        Never falls back to creation time: an old issue that only just finished
        must not look finished-24h-ago.
        """
        return max((parse_time(h["createdAt"]) for h in detail["history"]["nodes"]
                    if h["toStateId"] in self.finished_ids), default=None)

    def worktree_dirs(self, ident):
        """(dirs, rejected): real worktree dirs under work/<ID>/worktrees/, sorted.

        Symlinks are never followed and the directory must genuinely belong to
        this issue: worktrees/ itself may not be a symlink, and its real path
        must resolve inside work/<ID>/. The same holds per entry. Anything
        else is rejected and logged, never touched.
        """
        if not IDENT_RE.fullmatch(ident):
            return [], 0
        work_real = os.path.realpath(self.work)
        base = os.path.join(self.work, ident, "worktrees")
        if os.path.islink(base):
            self.say(f"prune-skip {ident}: worktrees/ is a symlink, refusing to touch")
            return [], 1
        base_real = os.path.realpath(base)
        if base_real != os.path.join(work_real, ident, "worktrees"):
            self.say(f"prune-skip {ident}: worktrees/ does not resolve inside {ident}/, refusing to touch")
            return [], 1
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return [], 0
        dirs, rejected = [], 0
        for n in names:
            if n.startswith("."):
                continue
            p = os.path.join(base, n)
            if os.path.islink(p) or not os.path.isdir(p) or \
                    os.path.realpath(p) != os.path.join(base_real, n):
                self.say(f"prune-skip {ident}/{n}: not a real directory inside {ident}/, refusing to touch")
                rejected += 1
                continue
            dirs.append(p)
        return dirs, rejected

    def clone_of(self, path):
        """The clone that path is a linked worktree of, read from files only.

        A repository under work/ could be planted (its config can run programs),
        so path must not vouch for itself: its .git file must name
        <clone>/.git/worktrees/<n> of a clone outside work/, which points back.
        """
        dotgit = os.path.join(path, ".git")
        text = "" if os.path.islink(dotgit) else _read(dotgit)
        if not text.startswith("gitdir: "):
            raise Skip("not a linked worktree (.git is not a gitdir file)")
        gitdir = os.path.realpath(os.path.join(path, text[len("gitdir: "):]))
        common = os.path.dirname(os.path.dirname(gitdir))
        clone = os.path.dirname(common)
        work_real = os.path.realpath(self.work)
        if os.path.basename(os.path.dirname(gitdir)) != "worktrees" or os.path.basename(common) != ".git":
            raise Skip("gitdir is not <clone>/.git/worktrees/<name>")
        if clone == work_real or clone.startswith(work_real + os.sep):
            raise Skip("its clone is inside work/")
        back = _read(os.path.join(gitdir, "gitdir"))
        if not back or os.path.realpath(os.path.join(gitdir, back)) != dotgit:
            raise Skip("the clone does not point back to this worktree")
        return clone

    def registered_branch(self, clone, path):
        """The branch checked out at path, from the clone's own worktree list."""
        for rec in self._git(clone, "worktree", "list", "--porcelain", "-z").split("\0\0"):
            fields = rec.split("\0")
            if fields[0].startswith("worktree ") and os.path.realpath(fields[0][len("worktree "):]) == path:
                refs = [f[len("branch refs/heads/"):] for f in fields if f.startswith("branch refs/heads/")]
                if not refs:
                    raise Skip("detached HEAD")
                return refs[0]
        raise Skip("not a registered worktree")

    def inspect(self, wt):
        """The Plan for removing wt; raises Skip if it must stay.

        A real run fetches the remote branch into its tracking ref first:
        `branch -d` judges "fully merged" against that ref, so a stale one would
        strand the branch once the worktree is gone.
        """
        path = os.path.realpath(wt)
        clone = self.clone_of(path)
        branch = self.registered_branch(clone, path)
        if not REF.fullmatch(branch):
            raise Skip("unsafe branch name")
        status = self._git(path, "status", "--porcelain", "-z", "--ignored", "--untracked-files=all")
        entries = [e for e in status.split("\0") if e]
        if any(not e.startswith("!! ") for e in entries):
            raise Skip("uncommitted changes")
        kept = [e[3:] for e in entries if not regenerable(e[3:])]
        if kept:
            raise Skip(f"{len(kept)} ignored file(s) would be lost, e.g. {kept[0]}")
        try:
            upstream = self._git(path, "rev-parse", "--symbolic-full-name", "@{u}").strip()
        except TransientError:
            raise Skip("no upstream: cannot verify the branch was pushed") from None
        m = re.fullmatch(r"refs/remotes/([^/]+)/(.+)", upstream)
        if not m:
            raise Skip(f"cannot parse upstream {upstream}")
        remote, rbranch = m.groups()
        if not (REF.fullmatch(remote) and REF.fullmatch(rbranch)):
            raise Skip(f"unsafe upstream {upstream}")
        # Never trust the cached tracking ref: ask the actual remote.
        try:
            ls = self._git(path, "ls-remote", "--", remote, f"refs/heads/{rbranch}")
        except TransientError:
            raise Skip(f"cannot reach the remote to confirm {upstream}") from None
        tips = [ln.split()[0] for ln in ls.splitlines() if ln.split()]
        if not tips:
            raise Skip(f"cannot confirm {upstream} on the remote")
        tip = tips[0]
        if not self.dry:
            self._git(clone, "fetch", "--", remote, f"+refs/heads/{rbranch}:{upstream}")
            tip = upstream
        res = self._run(path, "merge-base", "--is-ancestor", "HEAD", tip)
        if res.returncode == 1:
            raise Skip("unpushed commits")
        if res.returncode != 0:
            raise Skip(f"cannot compare with the remote tip: {_stderr(res)}")
        return Plan(path, clone, branch)

    def remove(self, wt, plan):
        # git resolves the path again: refuse if wt was swapped since inspect().
        if os.path.islink(wt) or os.path.realpath(wt) != plan.path:
            raise Skip("changed since it was checked, refusing to touch")
        self._git(plan.clone, "worktree", "remove", plan.path)
        self._git(plan.clone, "branch", "-d", plan.branch)

    def prune_issue(self, ident):
        """(cleaned, skipped, errors) for one finished issue."""
        cleaned, errors = 0, 0
        wts, skipped = self.worktree_dirs(ident)
        for wt in wts:
            name = os.path.basename(wt)
            try:
                plan = self.inspect(wt)
                if self.dry:
                    self.say(f"prune-plan {ident}/{name}: remove worktree, delete local branch {plan.branch}")
                else:
                    self.remove(wt, plan)
                    self.say(f"prune-removed {ident}/{name}: worktree removed, local branch {plan.branch} deleted")
                cleaned += 1
            except Skip as e:
                self.say(f"prune-skip {ident}/{name}: {e}")
                skipped += 1
            except TransientError as e:
                self.say(f"prune-error {ident}/{name}: {e}")
                errors += 1
        return cleaned, skipped, errors

    def worktree_idents(self):
        """Sorted issue identifiers with a non-empty work/<ID>/worktrees/ dir."""
        try:
            names = sorted(os.listdir(self.work))
        except OSError:
            return []
        out = []
        for n in names:
            if not IDENT_RE.fullmatch(n):
                continue
            wt = os.path.join(self.work, n, "worktrees")
            try:
                entries = os.listdir(wt)
            except OSError:
                continue
            if any(not e.startswith(".") and os.path.isdir(os.path.join(wt, e)) for e in entries):
                out.append(n)
        return out

    def run(self):
        idents = self.worktree_idents()
        if not idents:  # the common case: no Linear query, no log line
            return 0
        self.setup()
        cleaned, skipped, errors = 0, 0, 0
        for ident in idents:
            try:
                issue = self.gql(Q_ISSUE, i=ident)["issue"]
            except (Exception, SystemExit) as e:  # linear_gql raises SystemExit on API errors
                self.say(f"prune-error {ident}: Linear: {e}")
                errors += 1
                continue
            if issue is None or (issue.get("state") or {}).get("name") not in FINISHED:
                continue
            since = self.finished_since(issue)
            if since is None:
                self.say(f"prune-skip {ident}: finish time unknown, refusing to prune")
                skipped += 1
                continue
            if self.now - since < QUARANTINE:
                continue
            c, s, e = self.prune_issue(ident)
            cleaned, skipped, errors = cleaned + c, skipped + s, errors + e
        if cleaned or skipped or errors:
            self.say(f"prune: done (cleaned {cleaned}, skipped {skipped}, errors {errors})")
        return 3 if errors else 0


def main(argv, gql=linear_gql, run=sh_run, work=WORK, now=None):
    import argparse
    ap = argparse.ArgumentParser(prog="prune.py")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        pruner = Pruner(gql, load_config(), now or datetime.now().astimezone(), a.dry_run,
                        run=run, work=work)
        return pruner.run()
    except SystemExit as e:
        if str(e).startswith("linear api error"):
            print(f"prune.py: transient: {e}", file=sys.stderr)
            return 3
        raise
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"prune.py: transient: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
