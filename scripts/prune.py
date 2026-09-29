#!/usr/bin/env python3
"""Prune worktrees of finished issues (TASK-49).

For each issue Done or Canceled for at least 24 hours (by its history; unknown
means skip), removes each worktree under work/<ID>/worktrees/ with `git worktree
remove` (never --force) and deletes its local branch, but only if it has no
uncommitted changes (untracked files count), no unpushed commits, and is
registered with a clone directly in ~/playground (eng.py's layout). Ignored
files (build output, .env) are deleted with the worktree. Anything else is
skipped and logged, once per reason (logs/prune-skips.json). Remote branches,
main workspaces and work/<ID>/ itself are never touched.

Runs at the end of promote's tick (every 15 minutes); a prune failure is logged
and never breaks promote. Linear is only queried when a worktree exists on
disk, so an idle tick costs nothing.

--dry-run   Print the plan and every skip; change nothing. It never fetches, so a
            worktree whose remote tip is not in the clone yet shows as not comparable.
Needs Python 3.11+ (tomllib).
Exit 0 = done (skips are normal), 2 = bad arguments, 3 = a transient failure
(Linear, git or the network); it is retried on the next run.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eng import PLAYGROUND, REF, SHORT, TransientError, _stderr, in_playground, sh_run  # noqa: E402
from pipeline import LOGS, WORK, linear_gql, load_config, parse_time  # noqa: E402

QUARANTINE = timedelta(hours=24)
FINISHED = ("Done", "Canceled")
IDENT_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")
SKIPS = os.path.join(LOGS, "prune-skips.json")
# Config may name programs git runs (fsmonitor, hooks, ext:: URLs); prune runs none of them.
# quotePath: a non-UTF-8 path prints escaped, not as bytes text mode can't decode.
SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
        "-c", "core.quotePath=true")
# For ls-remote/fetch; `-c remote.<name>.uploadpack` would not do: git uses that key's first value.
UPLOAD_PACK = "--upload-pack=git-upload-pack"

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


def _cmd(cwd, args):
    return f"git -C {cwd} {' '.join(args)}"[:120]


def _read(path):
    """A small regular file's stripped text, or "" (never blocks on a FIFO or device)."""
    try:
        if os.path.isfile(path):
            with open(path) as f:
                return f.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Pruner:
    def __init__(self, gql, cfg, now, dry, run=sh_run, work=WORK, playground=PLAYGROUND, skips=SKIPS):
        self.gql, self.cfg, self.now, self.dry = gql, cfg, now, dry
        self.git_run, self.work, self.playground, self.skips_path = run, work, playground, skips
        self.skips, self.seen, self.logged = {}, {}, False

    def setup(self):
        nodes = self.gql(Q_SETUP, t=self.cfg["team"])["workflowStates"]["nodes"]
        states = {s["name"]: s["id"] for s in nodes}
        missing = [s for s in FINISHED if s not in states]
        if missing:
            raise SystemExit(f"not found in Linear: {', '.join(missing)}")
        self.finished_ids = {states[s] for s in FINISHED}

    def say(self, msg):
        self.logged = True
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def skip(self, key, reason):
        """Log a skip only if it is new or its reason changed since the last run."""
        self.skips[key] = reason
        if self.seen.get(key) != reason:
            self.say(f"prune-skip {key}: {reason}")

    def _run(self, cwd, *args):
        """The git result; TransientError if git cannot run or its output cannot be decoded."""
        try:
            return self.git_run(["git", *SAFE, "-C", cwd, *args], SHORT)
        except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError) as e:
            raise TransientError(f"{_cmd(cwd, args)}: {type(e).__name__}") from None

    def _git(self, cwd, *args):
        """stdout; TransientError unless git exits 0."""
        res = self._run(cwd, *args)
        if res.returncode != 0:
            raise TransientError(f"{_cmd(cwd, args)}: {_stderr(res)}")
        return res.stdout

    def finished_since(self, detail):
        """When the issue last entered Done/Canceled, or None if the history doesn't say.

        Never falls back to creation time: an old issue that only just finished
        must not look finished-24h-ago.
        """
        return max((parse_time(h["createdAt"]) for h in detail["history"]["nodes"]
                    if h["toStateId"] in self.finished_ids), default=None)

    def worktree_dirs(self, ident):
        """Real worktree dirs under work/<ID>/worktrees/, sorted.

        Symlinks are never followed and the directory must genuinely belong to
        this issue: worktrees/ itself may not be a symlink, and its real path
        must resolve inside work/<ID>/. The same holds per entry. Anything
        else is skipped, never touched.
        """
        if not IDENT_RE.fullmatch(ident):
            return []
        work_real = os.path.realpath(self.work)
        base = os.path.join(self.work, ident, "worktrees")
        if os.path.islink(base):
            self.skip(ident, "worktrees/ is a symlink, refusing to touch")
            return []
        base_real = os.path.realpath(base)
        if base_real != os.path.join(work_real, ident, "worktrees"):
            self.skip(ident, f"worktrees/ does not resolve inside {ident}/, refusing to touch")
            return []
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return []
        dirs = []
        for n in names:
            if n.startswith("."):
                continue
            p = os.path.join(base, n)
            if os.path.islink(p) or not os.path.isdir(p) or \
                    os.path.realpath(p) != os.path.join(base_real, n):
                self.skip(f"{ident}/{n}", f"not a real directory inside {ident}/, refusing to touch")
                continue
            dirs.append(p)
        return dirs

    def clone_of(self, path):
        """The clone that path is a linked worktree of, read from files only.

        A repository under work/ could be planted (its config can run programs),
        so path must not vouch for itself: its .git file must name
        <clone>/.git/worktrees/<n> of a clone directly in the playground dir,
        which points back.
        """
        dotgit = os.path.join(path, ".git")
        text = "" if os.path.islink(dotgit) else _read(dotgit)
        if not text.startswith("gitdir: "):
            raise Skip("not a linked worktree (.git is not a gitdir file)")
        gitdir = os.path.realpath(os.path.join(path, text[len("gitdir: "):]))
        common = os.path.dirname(os.path.dirname(gitdir))
        if os.path.basename(os.path.dirname(gitdir)) != "worktrees" or os.path.basename(common) != ".git":
            raise Skip("gitdir is not <clone>/.git/worktrees/<name>")
        clone = os.path.dirname(common)
        if not in_playground(clone, self.playground):
            raise Skip(f"its clone {clone} is not directly in {self.playground}")
        back = _read(os.path.join(gitdir, "gitdir"))
        if not back or os.path.realpath(os.path.join(gitdir, back)) != dotgit:
            raise Skip("the clone does not point back to this worktree")
        return clone

    def registered_branch(self, clone, path):
        """The branch checked out at path, from the clone's own worktree list."""
        # -z: fields ("worktree <path>", "HEAD <sha>", "branch <ref>" or "detached", ...) end in NUL, records in two.
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
        if self._git(path, "status", "--porcelain", "--untracked-files=all").strip():
            raise Skip("uncommitted changes")
        fields = self._git(clone, "for-each-ref", "--format=%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)",
                           f"refs/heads/{branch}").rstrip("\n").split("\0")
        if len(fields) != 3 or not fields[0]:
            raise Skip("no upstream: cannot verify the branch was pushed")
        upstream, remote, merge = fields
        if not (upstream.startswith("refs/remotes/") and merge.startswith("refs/heads/")):
            raise Skip(f"upstream {upstream} is not a remote branch")
        if not all(REF.fullmatch(s) for s in (upstream, remote, merge[len("refs/heads/"):])):
            raise Skip(f"unsafe upstream {upstream}")
        # Never trust the cached tracking ref: ask the actual remote.
        try:
            ls = self._git(clone, "ls-remote", UPLOAD_PACK, "--", remote, merge)
        except TransientError:
            raise Skip(f"cannot reach the remote to confirm {upstream}") from None
        tips = [ln.split()[0] for ln in ls.splitlines() if ln.split()]
        if not tips:
            raise Skip(f"cannot confirm {upstream} on the remote")
        tip = tips[0]
        if not self.dry:
            self._git(clone, "fetch", UPLOAD_PACK, "--", remote, f"+{merge}:{upstream}")
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
        """(cleaned, errors) for one finished issue."""
        cleaned, errors = 0, 0
        for wt in self.worktree_dirs(ident):
            key = f"{ident}/{os.path.basename(wt)}"
            try:
                plan = self.inspect(wt)
                if self.dry:
                    self.say(f"prune-plan {key}: remove worktree, delete local branch {plan.branch}")
                else:
                    self.remove(wt, plan)
                    self.say(f"prune-removed {key}: worktree removed, local branch {plan.branch} deleted")
                cleaned += 1
            except Skip as e:
                self.skip(key, str(e))
            except TransientError as e:
                self.say(f"prune-error {key}: {e}")
                errors += 1
        return cleaned, errors

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

    def prune_all(self):
        """(cleaned, errors) over the finished issues with worktrees on disk."""
        idents = self.worktree_idents()
        if not idents:  # the common case: no Linear query
            return 0, 0
        self.setup()
        cleaned, errors = 0, 0
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
                self.skip(ident, "finish time unknown, refusing to prune")
                continue
            if self.now - since < QUARANTINE:
                continue
            c, e = self.prune_issue(ident)
            cleaned, errors = cleaned + c, errors + e
        return cleaned, errors

    def run(self):
        if not self.dry:  # a dry run logs every skip and records none
            self.seen = _load(self.skips_path)
        cleaned, errors = self.prune_all()
        if not self.dry and self.skips != self.seen:
            with open(self.skips_path, "w") as f:
                json.dump(self.skips, f)
        if self.logged:
            self.say(f"prune: done (cleaned {cleaned}, skipped {len(self.skips)}, errors {errors})")
        return 3 if errors else 0


def main(argv, gql=linear_gql, run=sh_run, work=WORK, now=None, playground=PLAYGROUND, skips=SKIPS):
    import argparse
    ap = argparse.ArgumentParser(prog="prune.py")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        pruner = Pruner(gql, load_config(), now or datetime.now().astimezone(), a.dry_run,
                        run=run, work=work, playground=playground, skips=skips)
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
