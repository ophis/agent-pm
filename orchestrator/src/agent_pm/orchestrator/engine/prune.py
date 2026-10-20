"""Prune, for each finished issue (Done, Canceled, or any other state of type canceled or duplicate,
such as Duplicate: ops.Team.finished) at least 2 hours ago (linear_tracker.finished_at; unknown: skipped) whose
workdir <work_dir>/work/<ID> its Delegate (its latest recorded run's kind, else the
configured one) reports agent files in (Delegate.checkouts not None):
- closes its recorded run through LinearRun; a failed close leaves the rest for a later tick;
- deletes its agent files, uncommitted work included, through its Delegate (Delegate.clean; its
  refusal skips the rest); the run records and the rest of the workdir stay until then;
- in the local clone of each worktree among its checkouts, unless that clone lies in
  <work_dir>/work or a temp dir: `worktree prune`, then deletes each <ID>-* branch but the default
  that origin/<branch> or origin/<default> holds, else keeps and reports it (git reads refs only,
  so code an agent run planted, such as filters or hooks, never runs outside auto mode's review);
- archives every finished issue of the team, a Done one only when assigned to the pm or engineer
  role account (a researcher's report stays), any other whatever its assignee, none included, unless
  a sub-issue, at any depth, is unfinished (Linear archives sub-issues with their parent, theirs
  too): skipped until each is finished or archived.

For each stalled issue (any state but Todo, In Progress or a finished one) whose latest move into its
state and latest recorded run are both at least 7 days old, it deletes its agent files' checkouts and
temp files through its Delegate (Delegate.clean scratch; its refusal, a run or agent session still
open included, skips it), keeping the rest and its run records, closing no run; then only `worktree
prune` in each worktree's local clone, keeping every branch (it may back an open PR).

For each issue finished at least 7 days ago whose workdir is a real dir without agent files
(and of a configured kind), it deletes the whole workdir (run records, write-back ledger) once its
Delegate's clean finds no live run (its refusal skips it). An issue without agent files is not
attached; one of a kind no longer configured is neither queried nor logged. Run inventory unknown: an
error, and no issue is pruned. Each step is logged (event_log.log, src prune); a failure is logged
and the rest goes on (run returns 3), but Unavailable is raised. Promote prunes after each of its
ticks; `agent-pm-orchestrator prune` (main, HELP) runs one alone.
"""

import functools
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.delegate import SessionError
from agent_pm.orchestrator.engine import config, runs
from agent_pm.orchestrator.engine import log as event_log
from agent_pm.orchestrator.engine.flow.types import (
    ISSUE_ID,
    IssueState,
)
from agent_pm.orchestrator.engine.linear_run import LinearRun
from agent_pm.orchestrator.engine.log import linear_error, log
from agent_pm.orchestrator.engine.types import (
    ConfigError,
    LinearError,
    Unavailable,
)
from agent_pm.orchestrator.trackers.linear.client import linear_gql
from agent_pm.orchestrator.trackers.linear.linear_tracker import LinearTracker, team_and_roles
from agent_pm.utils import (
    GUARD,
    SHORT,
    GitError,
    StatusState,
    common_dir,
    one_line,
    remove_tree,
    under,
)
from agent_pm.utils import run_git as git

QUARANTINE = timedelta(hours=2)
RETENTION = timedelta(days=7)
STALL = timedelta(days=7)
IDENT_RE = re.compile(ISSUE_ID)
DELETES = {False: "agent dir", True: "checkouts and temp files"}  # by remove_agent's scratch
ARCHIVE_ROLES = ("pm", "engineer")
HELP = """Clean finished issues' recorded runs and files, then archive eligible issues.

Usage: agent-pm-orchestrator prune [--dry-run]

Arguments and options:
  --dry-run   Print the cleanup plan without closing, deleting or archiving.
              Read Linear, configuration, local runs/checkouts and Delegate
              status; this is not an offline or credentials-free preview.
  -h, --help  Print this help alone; exit 0.
  --dry-run may repeat with no further effect; other arguments exit 2.

Prerequisites and effects:
  Requires configured Linear/Delegate settings, Linear access,
  linear.harness_key Keychain credentials and access to the configured
  work_dir/local clones.
  Configuration and executable lookup: agent-pm-orchestrator --help
  For issues Done/Canceled/Duplicate (any canceled-type state) at least 2
  hours (the latest of their move into one and Linear's canceledAt and
  completedAt; unknown age is skipped), close recorded runs and
  delete agent files, including uncommitted work, through their Delegate.
  Close/cleanup refusal preserves later steps for a future tick. Unknown run
  inventory prevents run/file pruning; archival still proceeds.
  Prune worktree records in each checkout's local clone and remove pushed
  ISSUE-* branches already contained in origin/branch or origin/default;
  preserve the default branch and unpushed branches. No fetch is performed.
  Clones under work_dir/work or temporary directories are skipped.
  Archive team issues finished at least 2 hours ago: Canceled/Duplicate
  whatever the assignee (none or researcher too), Done only when assigned to
  pm/engineer role accounts (researcher reports stay). Unknown finish times
  are skipped, and so is an issue with an unfinished sub-issue at any depth
  (archiving it archives its sub-issues and theirs).
  For issues in any other state but Todo/In Progress, 7 days after both their
  latest move into it and their latest recorded run, delete their checkouts
  and temp files through their Delegate, keeping the other agent files and the
  run records; an open run or agent session skips it. Prune worktree records
  only; every branch is kept.
  After 7 days, remove whole workdirs without agent files, including run records
  and delivery ledgers, only when their configured Delegate confirms no live
  run. Unconfigured run kinds are skipped.
  Events go to orchestrator.jsonl (src prune); preview JSON lines go to stderr.
  Exit 0: completed; 1: Linear unavailable (logged once a day), unknown
  inventory or failed step after remaining steps; 2: configuration error
  (logged once a day) or bad arguments.
  Promote also runs this cleanup after each tick.

Example (configured Linear access and finished issues):
  agent-pm-orchestrator prune --dry-run
  Inspect the plan before running agent-pm-orchestrator prune to delete/archive.

Recovery and next help:
  Resolve logged close/cleanup refusals or unavailable inventory, then retry
  the preview. Back up agent work before allowing cleanup.
  Promotion and its cleanup: agent-pm-orchestrator promote --help
  Command discovery: agent-pm-orchestrator --help
"""


class Pruner:
    def __init__(self, gql, now, *, dry, run=None, work, team, roles, cfg, writable=None):
        self.now, self.dry, self.work, self.roles = now, dry, work, roles
        self.tracker = LinearTracker.of_team(gql, cfg, team, roles)
        self.run_git = run or config.shell
        self.cfg = cfg
        self.writable = config.writable(work) if writable is None else writable
        self.errors, self.views, self.classes, self.by_issue = 0, {}, {}, None
        self.say = functools.partial(log, "prune", dry=dry)

    def error(self, issue=None, **fields):
        self.errors += 1
        self.say("prune-error", issue, **fields)

    def clean_clone(self, ident, common, *, branches=True):
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
            pushed = f" and pushed {ident}-* branches" if branches else ""
            self.say("prune-plan", ident, what=f"prune {clone}'s worktree records{pushed}")
            return
        at = ["git", *GUARD, "-C", clone, f"--git-dir={common}"]
        try:
            git(clone, at[-1], "worktree", "prune", run=self.run_git, pre=GUARD)
            if not branches:
                return
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

    def inventory(self):
        """A run whose view cannot load has nothing to close, logged once; inventory unknown: None."""
        found = {}
        try:
            latest = {ident: runs.latest(rows)[ident] for ident, rows in self.log().items()}
        except (OSError, ValueError):
            return None
        for ident, entry in sorted(latest.items()):
            if entry.role not in self.roles.values() or not self.has_agent(ident, quiet=True):
                continue
            try:
                view = LinearRun.attach(ident, cfg=self.cfg, sid=entry.sid)
                if view.status().state == StatusState.UNKNOWN:
                    return None
            except (config.ConfigError, DelegateConfigError, OSError, ValueError) as error:
                self.say("prune-skip", ident, once=True, sid=entry.sid, reason=one_line(error))
                continue
            except SessionError:
                return None
            found[ident] = view
        return found

    def close_sessions(self, ident):
        view = self.views.get(ident)
        if view is None:
            return True
        if self.dry:
            self.say("prune-plan", ident, session=view.sid, what="close the run")
            return True
        try:
            view.delegate.close()
        except (
            config.ConfigError,
            DelegateConfigError,
            SessionError,
            OSError,
            ValueError,
        ) as error:
            self.error(ident, session=view.sid, error=one_line(error))
            return False
        self.say("prune-closed", ident, session=view.sid)
        return True

    def archive(self):
        try:
            found = self.tracker.finished(ARCHIVE_ROLES)
        except Unavailable:
            raise
        except (LinearError, OSError, subprocess.SubprocessError) as e:
            self.error(error=f"archive: Linear: {one_line(e)}")
            return
        for issue in found:
            ident, since = issue.ident, issue.since
            if since is None:
                self.say("prune-skip", ident, once=True, reason="archive: finish time unknown")
                continue
            if self.now - since < QUARANTINE:
                continue
            try:
                open_child = self.open_descendant(issue.children)
                if open_child is None and not self.dry:
                    self.tracker.archive(issue.key)
            except Unavailable:
                raise
            except (LinearError, OSError, subprocess.SubprocessError) as e:
                self.error(ident, error=f"archive: {one_line(e)}")
                continue
            if open_child:
                self.say(
                    "prune-skip",
                    ident,
                    once=True,
                    reason=f"archive: sub-issue {open_child} not finished",
                )
            elif self.dry:
                self.say("prune-plan", ident, what="archive")
            else:
                self.say("prune-archived", ident)

    def open_descendant(self, children):
        """The first unfinished one of children and, recursively, their sub-issues; None: all
        finished or archived (the tracker lists no archived sub-issue)."""
        for child in children:
            if not child.finished:
                return child.ident
        for child in children:
            if found := self.open_descendant(self.tracker.sub_issues(child.key)):
                return found
        return None

    def run(self):
        found = self.inventory()
        self.views = found or {}
        if found is None:
            self.error(error="run inventory unknown")
        else:
            try:
                idents = [
                    n
                    for n in sorted(os.listdir(self.work))
                    if IDENT_RE.fullmatch(n) and (self.has_agent(n) or self.bare(n))
                ]
            except OSError:
                idents = []
            for ident in idents:
                self.prune_issue(ident)
        self.archive()
        return 3 if self.errors else 0

    def prune_issue(self, ident):
        try:
            found = self.tracker.read(ident)
        except Unavailable:
            raise
        except (LinearError, OSError, subprocess.SubprocessError) as e:
            self.error(ident, error=f"Linear: {one_line(e)}")
            return
        if found is None or found.state in (IssueState.READY, IssueState.RUNNING):
            return
        if found.state not in (IssueState.DONE, IssueState.CANCELED):
            self.prune_stalled(ident, found.since)
            return
        since = found.since
        if since is None:
            self.say("prune-skip", ident, once=True, reason="finish time unknown")
        elif self.has_agent(ident, quiet=True):
            if self.now - since >= QUARANTINE and self.close_sessions(ident):
                self.remove_agent(ident)
        elif self.now - since >= RETENTION:
            self.remove_workdir(ident)

    def prune_stalled(self, ident, since):
        if since is None:
            self.say("prune-skip", ident, once=True, reason="stall time unknown")
            return
        since = max([since, *(e.ts for e in self.log().get(ident, ()))])
        if self.now - since >= STALL and self.has_agent(ident, quiet=True):
            self.remove_agent(ident, scratch=True)

    def log(self):
        """runs.jsonl's entries by issue, read once per tick."""
        if self.by_issue is None:
            by_issue = {}
            for entry in runs.parse_log(self.cfg.runs_log):
                by_issue.setdefault(entry.issue, []).append(entry)
            self.by_issue = by_issue
        return self.by_issue

    def delegate(self, ident):
        """The issue's Delegate class, once per tick: its latest recorded run's kind, else the
        configured one; ConfigError (a kind no longer configured) is kept.
        """
        if ident not in self.classes:
            try:
                rows = self.log().get(ident)
                self.classes[ident] = (
                    LinearRun.kind_cls(
                        self.cfg, runs.delegate_of(rows, runs.latest(rows)[ident].sid)
                    )
                    if rows
                    else self.cfg.delegate_cls
                )
            except (config.ConfigError, OSError, ValueError) as error:
                self.classes[ident] = error
        return self.classes[ident]

    def has_agent(self, ident, *, quiet=False):
        found, workdir = self.delegate(ident), os.path.join(self.work, ident)
        if isinstance(found, Exception):
            if not quiet and self.cfg.delegate_cls.checkouts(workdir) is not None:
                self.say("prune-skip", ident, once=True, reason=one_line(found))
            return False
        return found.checkouts(workdir) is not None

    def bare(self, ident):
        """A real workdir without agent files, of a configured kind."""
        workdir = os.path.join(self.work, ident)
        return (
            not isinstance(self.delegate(ident), Exception)
            and os.path.isdir(workdir)
            and not os.path.islink(workdir)
        )

    def remove_workdir(self, ident):
        workdir = os.path.join(self.work, ident)
        try:
            self.delegate(ident).clean(workdir, dry=self.dry)
            if not self.dry:
                remove_tree(workdir)
        except (config.ConfigError, DelegateConfigError) as error:
            self.say("prune-skip", ident, once=True, reason=one_line(error))
            return
        except (SessionError, OSError) as error:
            self.error(ident, error=f"remove: {one_line(error)}")
            return
        if self.dry:
            self.say("prune-plan", ident, what="delete the workdir")
        else:
            self.say("prune-removed", ident, what="workdir")

    def remove_agent(self, ident, *, scratch=False):
        found, workdir = self.delegate(ident), os.path.join(self.work, ident)
        commons = {c for wt in found.checkouts(workdir) or () if (c := common_dir(wt))}
        view = self.views.get(ident)
        try:
            if scratch:
                if not found.clean(workdir, dry=self.dry, scratch=True):
                    return
            elif not (self.dry and view is not None and view.delegate.alive()):
                found.clean(workdir, dry=self.dry)
        except (config.ConfigError, DelegateConfigError) as error:
            self.say("prune-skip", ident, once=True, reason=one_line(error))
            return
        except (SessionError, OSError) as error:
            self.error(ident, error=f"clean: {one_line(error)}")
            return
        if self.dry:
            self.say("prune-plan", ident, what=f"delete the {DELETES[scratch]}")
        else:
            self.say("prune-removed", ident, what="scratch" if scratch else "agent dir")
        for common in sorted(commons):
            self.clean_clone(ident, common, branches=not scratch)


def main(argv, *, gql=linear_gql, now=None, config_path=None, pruner=Pruner):
    if argv in (["--help"], ["-h"]):
        print(HELP)
        return 0
    if any(a != "--dry-run" for a in argv):
        print(HELP, file=sys.stderr)
        return 2
    dry = "--dry-run" in argv
    os.environ["PATH"] = config.path()
    try:
        cfg = config.load(path=config_path)
    except ConfigError as error:
        config.report_error("prune", error, dry=dry)
        return 2
    event_log.use_logs(cfg.logs_dir)
    gql = functools.partial(gql, service=cfg.harness_key)
    try:
        team, roles = team_and_roles(gql, cfg)
        code = pruner(
            gql,
            now or datetime.now(UTC),
            dry=dry,
            work=cfg.workdirs,
            team=team,
            roles=roles,
            cfg=cfg,
        ).run()
    except LinearError as e:
        return event_log.config_error("prune", e, dry=dry)
    except Unavailable as e:
        return linear_error("prune", e, dry=dry)
    return 1 if code else 0
