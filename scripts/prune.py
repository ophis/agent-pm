#!/usr/bin/env python3
"""Prune worktrees of finished issues (TASK-49).

For every issue in Done or Canceled for at least 24 hours (an undo window for
reopens; the finish time must be confirmed from the issue history, otherwise
the issue is skipped): for each real directory under work/<ID>/worktrees/
(symlinks are never followed, nothing may escape the issue's own directory),
if the worktree has no uncommitted changes and the actual remote is confirmed
(via ls-remote, never the cached remote-tracking ref) to contain its commits,
remove the worktree (`git worktree remove`, never --force) and delete the
local branch. Remote branches and every repo's main workspace are never
touched. work/<ID>/ itself is kept: `cd work/<ID> && claude --resume <sid>`
(TASK-26) depends on it, as do the session logs.

Runs at the end of promote's tick (every 15 minutes); a prune failure is logged
and never breaks promote. Linear is only queried when a worktree exists on
disk, so an idle tick costs nothing.

--dry-run   Print the plan; change nothing.
Needs Python 3.11+ (tomllib).
Exit 0 = done (skips are normal), 2 = bad arguments, 3 = a top-level transient
failure (Linear or git unusable). A worktree that fails transiently is logged
and left for the next run.
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import CONFIG, PATH, WORK, linear_gql, load_config, parse_time  # noqa: E402

QUARANTINE = timedelta(hours=24)
FINISHED = ("Done", "Canceled")
SHORT = 60
IDENT_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")
REF_RE = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+")

Q_SETUP = """query($t: String!) {
  teams(filter: { name: { eq: $t } }) { nodes { id } }
  workflowStates(filter: { team: { name: { eq: $t } } }) { nodes { id name } } }"""
Q_ISSUE = """query($i: String!) { issue(id: $i) { identifier state { name } createdAt
  history(first: 100) { nodes { createdAt toStateId } } } }"""


class TransientError(Exception):
    """A git call failed unexpectedly: the worktree is left for the next run."""


def sh_run(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "PATH": PATH})


def _stderr(res):
    return (res.stderr or "").strip()[:200]


class Pruner:
    def __init__(self, gql, cfg, now, dry, run=sh_run, work=WORK):
        self.gql, self.cfg, self.now, self.dry = gql, cfg, now, dry
        self.git_run, self.work = run, work
        setup = gql(Q_SETUP, t=cfg["team"])
        self.states = {s["name"]: s["id"] for s in setup["workflowStates"]["nodes"]}
        missing = [s for s in FINISHED if s not in self.states]
        if missing:
            raise SystemExit(f"not found in Linear: {', '.join(missing)}")
        self.finished_ids = {self.states[s] for s in FINISHED}

    def say(self, msg):
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def _git(self, wt, *args):
        """Run git in wt; raise TransientError unless it exits 0. Returns stdout."""
        argv = ["git", "-C", wt, *args]
        try:
            res = self.git_run(argv, SHORT)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            raise TransientError(f"{' '.join(argv)[:120]}: {type(e).__name__}") from None
        if res.returncode != 0:
            raise TransientError(f"{' '.join(argv)[:120]}: {_stderr(res)}")
        return res.stdout

    def _git_ok(self, wt, *args):
        """True if the git command exits 0; never raises."""
        try:
            res = self.git_run(["git", "-C", wt, *args], SHORT)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False
        return res.returncode == 0

    def finished_since(self, detail):
        """When the issue last entered Done/Canceled, or None if it cannot be confirmed.

        Never falls back to creation time: an old issue that only just finished
        must not look finished-24h-ago.
        """
        hist = [h for h in detail["history"]["nodes"] if h["toStateId"] in self.finished_ids]
        if not hist:
            return None
        hist = sorted(hist, key=lambda h: parse_time(h["createdAt"]))
        return parse_time(hist[-1]["createdAt"])

    def worktree_dirs(self, ident):
        """(dirs, rejected): real worktree dirs under work/<ID>/worktrees/, sorted.

        Symlinks are never followed and nothing may escape the issue's own
        directory: an entry must be a real directory whose real path sits
        directly inside work/<ID>/worktrees/. Anything else is rejected and
        logged, never touched.
        """
        if not IDENT_RE.fullmatch(ident):
            return [], 0
        work_real = os.path.realpath(self.work)
        base = os.path.join(self.work, ident, "worktrees")
        base_real = os.path.realpath(base)
        if not base_real.startswith(work_real + os.sep):
            self.say(f"prune-skip {ident}: worktrees dir escapes the work dir, refusing to touch")
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

    def inspect(self, wt):
        """("ok", branch, clone) if the worktree may be removed, else ("skip", reason)."""
        real = os.path.realpath(wt)
        base = os.path.realpath(os.path.join(self.work))
        if not real.startswith(base + os.sep):
            return "skip", "worktree path escapes the work dir"
        if self._git(wt, "rev-parse", "--is-inside-work-tree").strip() != "true":
            return "skip", "not a git worktree"
        listed = [line.split(" ", 1)[1] for line in
                  self._git(wt, "worktree", "list", "--porcelain").splitlines()
                  if line.startswith("worktree ")]
        if real not in [os.path.realpath(p) for p in listed]:
            return "skip", "not a registered worktree"
        branch = self._git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch == "HEAD" or not REF_RE.fullmatch(branch):
            return "skip", "detached HEAD or unsafe branch name"
        if self._git(wt, "status", "--porcelain").strip():
            return "skip", "uncommitted changes"
        # Never trust the cached remote-tracking ref: ask the actual remote
        # whether it contains our commits. Anything unconfirmable is skipped.
        try:
            upstream = self._git(wt, "rev-parse", "--symbolic-full-name", "@{u}").strip()
        except TransientError:
            return "skip", "no upstream: cannot verify the branch was pushed"
        m = re.fullmatch(r"refs/remotes/([^/]+)/(.+)", upstream)
        if not m:
            return "skip", f"cannot parse upstream {upstream}"
        remote, rbranch = m.group(1), m.group(2)
        try:
            ls = self._git(wt, "ls-remote", remote, f"refs/heads/{rbranch}")
        except TransientError:
            return "skip", f"cannot reach the remote to confirm {upstream}"
        tips = [ln.split()[0] for ln in ls.splitlines() if ln.split()]
        if not tips:
            return "skip", f"cannot confirm {upstream} on the remote"
        if not self._git_ok(wt, "merge-base", "--is-ancestor", "HEAD", tips[0]):
            return "skip", "unpushed commits"
        clone = os.path.dirname(self._git(wt, "rev-parse", "--git-common-dir").strip())
        return "ok", branch, clone

    def remove(self, wt, branch, clone):
        self._git(clone, "worktree", "remove", os.path.realpath(wt))
        self._git(clone, "branch", "-d", branch)

    def prune_issue(self, ident):
        """(cleaned, skipped, errors) for one finished issue."""
        cleaned, skipped, errors = 0, 0, 0
        wts, rejected = self.worktree_dirs(ident)
        skipped += rejected
        for wt in wts:
            name = os.path.basename(wt)
            try:
                state, *rest = self.inspect(wt)
            except TransientError as e:
                self.say(f"prune-error {ident}/{name}: {e}")
                errors += 1
                continue
            if state == "skip":
                self.say(f"prune-skip {ident}/{name}: {rest[0]}")
                skipped += 1
                continue
            branch, clone = rest
            if self.dry:
                self.say(f"prune-plan {ident}/{name}: remove worktree, delete local branch {branch}")
                cleaned += 1
                continue
            try:
                self.remove(wt, branch, clone)
            except TransientError as e:
                self.say(f"prune-error {ident}/{name}: {e}")
                errors += 1
                continue
            self.say(f"prune-removed {ident}/{name}: worktree removed, local branch {branch} deleted")
            cleaned += 1
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
            if os.path.islink(os.path.join(self.work, n)):
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
        if not idents:  # the common case: Linear is not queried at all
            self.say("prune: nothing to do (no worktrees on disk)")
            return 0
        cleaned, skipped, errors, young = 0, 0, 0, 0
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
                young += 1
                continue
            c, s, e = self.prune_issue(ident)
            cleaned, skipped, errors = cleaned + c, skipped + s, errors + e
        if not (cleaned or skipped or errors):
            self.say(f"prune: nothing to do ({len(idents)} with worktrees on disk, {young} still in quarantine)")
        else:
            self.say(f"prune: done (cleaned {cleaned}, skipped {skipped}, errors {errors})")
        return 3 if errors else 0


def main(argv, gql=None, run=sh_run, work=WORK, now=None):
    import argparse
    ap = argparse.ArgumentParser(prog="prune.py")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if gql is None:
        gql = linear_gql
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
