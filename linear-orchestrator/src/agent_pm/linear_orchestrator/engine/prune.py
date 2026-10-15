"""Prune, for each issue Done or Canceled at least 24 hours ago (its latest move into either; unknown:
skipped) whose agent dir (Layout(<work_dir>/work/<ID>).agent) exists:
- closes its recorded run through LinearRun;
- deletes the agent dir whole, uncommitted work included, only while it is a real directory in
  place (else skipped, nothing deleted); the run dir and the rest of the workdir stay;
- in the local clone of each worktree the agent dir held at src/<owner>/<name>, unless that clone
  lies in <work_dir>/work or a temp dir: `worktree prune`, then deletes each <ID>-* branch but the
  default that origin/<branch> or origin/<default> holds, else keeps and reports it (git reads refs
  only, so code an agent run planted, such as filters or hooks, never runs outside auto mode's
  review);
- archives every finished issue of the team assigned to the pm or engineer role account.

An issue without an agent dir is neither attached, queried nor logged. Run inventory unknown: an
error, and no issue is pruned. Each step is logged (event_log.log, src prune); a failure is logged
and the rest goes on (run returns 3), but Unavailable is raised. Promote prunes after each of its
ticks; `agent-pm-linear prune` (main, HELP) runs one alone.
"""

import contextlib
import functools
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.delegate import Layout, SessionError
from agent_pm.linear_orchestrator.engine import config, runs
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.client import linear_gql
from agent_pm.linear_orchestrator.engine.config import CLONES
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.log import linear_error, log
from agent_pm.linear_orchestrator.engine.ops import HISTORY, call, last_move, role_ids, team
from agent_pm.linear_orchestrator.engine.types import (
    ISSUE_ID,
    ConfigError,
    LinearError,
    Unavailable,
)
from agent_pm.utils import GUARD, SHORT, GitError, common_dir, one_line, under
from agent_pm.utils import run_git as git

QUARANTINE = timedelta(hours=24)
IDENT_RE = re.compile(ISSUE_ID)
ARCHIVE_ROLES = ("pm", "engineer")
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
HELP = """usage: agent-pm-linear prune [--dry-run]

One prune tick: close finished issues' recorded runs, delete their agent dirs, archive them.
--dry-run   Change nothing; print what would happen.
--help      Print this; exit 0.
Events go to orchestrator.jsonl (src prune). A config error or Linear unavailable exits 1, logged once
a day; a failed step exits 1 after the rest; else 0. Bad arguments exit 2."""


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

    def inventory(self):
        """A run whose view cannot load has nothing to close, logged once; inventory unknown: None."""
        found = {}
        try:
            latest = runs.latest(runs.parse_log(self.cfg.runs_log))
        except (OSError, ValueError):
            return None
        for ident, entry in sorted(latest.items()):
            if entry.role not in self.roles.values() or not os.path.lexists(self.agent(ident)):
                continue
            try:
                view = LinearRun.attach(ident, cfg=self.cfg, sid=entry.sid)
                if view.status().state == "unknown":
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
            return
        if self.dry:
            self.say("prune-plan", ident, session=view.sid, what="close the run")
            return
        try:
            view.close()
        except (
            config.ConfigError,
            DelegateConfigError,
            SessionError,
            OSError,
            ValueError,
        ) as error:
            self.error(ident, session=view.sid, error=one_line(error))
            return
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
                    if IDENT_RE.fullmatch(n) and os.path.lexists(self.agent(n))
                ]
            except OSError:
                idents = []
            for ident in idents:
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
            self.remove_agent(ident)

    def agent(self, ident):
        return Layout(os.path.join(self.work, ident)).agent

    def remove_agent(self, ident):
        agent = self.agent(ident)
        real = Layout(os.path.join(os.path.realpath(self.work), ident)).agent
        if os.path.islink(agent) or not os.path.isdir(agent) or os.path.realpath(agent) != real:
            self.say(
                "prune-skip",
                ident,
                once=True,
                reason=f"not a real directory: {agent}, refusing to touch",
            )
            return
        commons = {c for wt in self.checkouts(real) if (c := common_dir(wt))}
        if self.dry:
            self.say("prune-plan", ident, what="delete the agent dir")
        else:
            try:
                shutil.rmtree(agent)
            except OSError as e:
                self.error(ident, error=f"rmtree: {one_line(e)}")
                return
            self.say("prune-removed", ident, what="agent dir")
        for common in sorted(commons):
            self.clean_clone(ident, common)

    @staticmethod
    def checkouts(agent):
        """src/<owner>/<name> under the real agent dir, none reached through a symlink."""
        src, found = os.path.join(agent, CLONES[0]), []
        with contextlib.suppress(OSError):
            for owner in sorted(os.listdir(src)):
                with contextlib.suppress(OSError):
                    names = sorted(os.listdir(os.path.join(src, owner)))
                    found += [os.path.join(src, owner, name) for name in names]
        return [path for path in found if os.path.realpath(path) == path]


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
        return 1
    event_log.use_logs(cfg.logs_dir)
    gql = functools.partial(gql, service=cfg.harness_key)
    try:
        code = pruner(
            gql,
            now or datetime.now(UTC),
            dry=dry,
            work=cfg.runs_dir,
            team=team(gql, cfg),
            roles=role_ids(gql, cfg.roles),
            cfg=cfg,
        ).run()
    except LinearError as e:
        return event_log.config_error("prune", e, dry=dry)
    except Unavailable as e:
        return linear_error("prune", e, dry=dry)
    return 1 if code else 0
