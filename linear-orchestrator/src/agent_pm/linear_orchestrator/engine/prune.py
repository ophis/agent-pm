"""Prune, for each issue Done or Canceled at least 24 hours ago (its latest move into either; unknown:
skipped):
- closes its recorded run through LinearRun before its checkouts go;
- deletes <work_dir>/work/<ID>/src/<owner>/*, publish and tmp, each a real directory inside its own
  folder, a src or publish entry only as a clone or worktree (.git a directory or a file),
  uncommitted work included;
- in a deleted worktree's local clone, unless it lies in <work_dir>/work or a temp dir: `worktree
  prune`, then deletes each <ID>-* branch but the default that origin/<branch> or origin/<default>
  holds, else keeps and reports it (git reads refs only, so code an agent run planted, such as
  filters or hooks, never runs outside auto mode's review);
- archives every finished issue of the team assigned to the pm or engineer role account.

Run inventory unknown: an error, and no issue is pruned. Each step is logged (event_log.log, src prune);
a failure is logged and the rest goes on (run returns 3), but Unavailable is raised.
"""

import contextlib
import functools
import os
import re
import shutil
import subprocess
from datetime import timedelta

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.delegate import DriverError
from agent_pm.linear_orchestrator.engine import config, runs
from agent_pm.linear_orchestrator.engine.config import CLONES
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.log import log
from agent_pm.linear_orchestrator.engine.ops import HISTORY, call, last_move
from agent_pm.linear_orchestrator.engine.types import ISSUE_ID, LinearError, Unavailable
from agent_pm.utils import GUARD, SHORT, GitError, common_dir, one_line, under
from agent_pm.utils import run_git as git

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(ISSUE_ID)
ARCHIVE_ROLES = ("pm", "engineer")
FOLDERS = (CLONES[0],)
PUBLISH = CLONES[1]
TMP = "tmp"  # delegate/team/principles.md: an agent run's temp files
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
    def __init__(self, gql, now, *, dry, run=None, work, team, roles, cfg, writable=None):
        self.gql, self.now, self.dry, self.work = gql, now, dry, work
        self.team, self.roles = team, roles
        self.run_git = run or config.shell
        self.cfg = cfg
        self.writable = config.writable(work) if writable is None else writable
        self.errors, self.views = 0, {}
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
                with contextlib.suppress(OSError):
                    found += [
                        (folder, n, m)
                        for m in sorted(os.listdir(entry))
                        if not m.startswith(".") or os.path.isdir(os.path.join(entry, m))
                    ]
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
            common = common_dir(path) if os.path.isfile(dotgit) else None
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
        if root := under(common, self.writable):
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
        at = ["git", *GUARD, "-C", clone, f"--git-dir={common}"]
        try:
            git(clone, at[-1], "worktree", "prune", run=self.run_git, pre=GUARD)
            head = self.run_git(
                [*at, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], SHORT
            )
            default = head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else None
            listed = git(
                clone,
                at[-1],
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/{ident}-*",
                run=self.run_git,
                pre=GUARD,
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
                        SHORT,
                    ).returncode
                    == 0
                    for b in dict.fromkeys(filter(None, (branch, default)))
                ):
                    git(clone, at[-1], "branch", "-D", branch, run=self.run_git, pre=GUARD)
                    self.say("prune-removed", ident, what=f"branch {branch} in {clone}")
                else:
                    self.say(
                        "prune-skip",
                        ident,
                        once=True,
                        reason=f"branch {branch} in {clone} kept: not pushed",
                    )
        except (GitError, OSError, subprocess.TimeoutExpired) as e:
            self.error(ident, error=one_line(e))

    def rmdir_owners(self, ident, owners):
        for owner in sorted(owners):
            with contextlib.suppress(OSError):
                os.rmdir(os.path.join(self.work, ident, *owner))

    def inventory(self):
        """A run whose view cannot load has nothing to close, logged once; inventory unknown: None."""
        found = {}
        try:
            latest = runs.latest(runs.parse_log(self.cfg.runs_log))
        except (OSError, ValueError):
            return None
        for ident, entry in sorted(latest.items()):
            if entry.role not in self.roles.values():
                continue
            try:
                view = LinearRun.attach(ident, cfg=self.cfg, sid=entry.sid)
                if view.status().state == "unknown":
                    return None
            except (config.ConfigError, DelegateConfigError, OSError, ValueError) as error:
                self.say("prune-skip", ident, once=True, sid=entry.sid, reason=one_line(error))
                continue
            except DriverError:
                return None
            found[ident] = view
        return found

    def close_sessions(self, ident):
        view = self.views.get(ident)
        if view is None:
            return
        if self.dry:
            self.say("prune-plan", ident, session=view.sid, what="close the run")
            return
        try:
            view.close()
        except (config.ConfigError, DelegateConfigError, DriverError, OSError, ValueError) as error:
            self.error(ident, session=view.sid, error=one_line(error))
        else:
            self.say("prune-closed", ident, session=view.sid)

    def finished_issues(self, finished):
        ids = [uid for uid, role in self.roles.items() if role in ARCHIVE_ROLES]
        if not ids:
            return []
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
        return nodes

    def archive(self, finished):
        try:
            nodes = self.finished_issues(finished)
        except Unavailable:
            raise
        except (LinearError, OSError, subprocess.SubprocessError) as e:
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
                except (LinearError, OSError, subprocess.SubprocessError) as e:
                    self.error(ident, error=f"archive: {one_line(e)}")
                    continue
                self.say("prune-archived", ident)

    def run(self):
        finished = {self.team.states["done"], self.team.states["canceled"]}
        found = self.inventory()
        self.views = found or {}
        if found is None:
            self.error(error="run inventory unknown")
        else:
            try:
                idents = [
                    n
                    for n in sorted(os.listdir(self.work))
                    if IDENT_RE.fullmatch(n)
                    and any(os.path.isdir(os.path.join(self.work, n, *p)) for p in self.entries(n))
                ]
            except OSError:
                idents = []
            for ident in sorted(set(idents) | set(self.views)):
                self.prune_issue(ident, finished)
        self.archive(finished)
        return 3 if self.errors else 0

    def prune_issue(self, ident, finished):
        try:
            issue = self.gql(Q_ISSUE, i=ident)["issue"]
        except Unavailable:
            raise
        except (LinearError, OSError, subprocess.SubprocessError) as e:
            self.error(ident, error=f"Linear: {one_line(e)}")
            return
        if not issue or issue["state"]["id"] not in finished:
            return
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
