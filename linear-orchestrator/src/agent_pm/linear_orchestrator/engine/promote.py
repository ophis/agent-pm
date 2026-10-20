import functools
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from traceback import format_exception

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.client import linear_gql

# Kept only for tests reading promote.<query>; 第 2 阶段删除.
from agent_pm.linear_orchestrator.engine.linear_tracker import M_CREATE as M_CREATE
from agent_pm.linear_orchestrator.engine.linear_tracker import M_RELATE as M_RELATE
from agent_pm.linear_orchestrator.engine.linear_tracker import Q_CHILD as Q_CHILD
from agent_pm.linear_orchestrator.engine.linear_tracker import Q_DETAIL as Q_DETAIL
from agent_pm.linear_orchestrator.engine.linear_tracker import Q_HANDOFF as Q_HANDOFF
from agent_pm.linear_orchestrator.engine.linear_tracker import LinearTracker
from agent_pm.linear_orchestrator.engine.log import linear_error, log
from agent_pm.linear_orchestrator.engine.ops import role_ids, team
from agent_pm.linear_orchestrator.engine.prune import Pruner
from agent_pm.linear_orchestrator.engine.types import ConfigError, LinearError, Unavailable
from agent_pm.linear_orchestrator.flow.handoff import handoff as handoff_text
from agent_pm.linear_orchestrator.flow.types import Approved, Detail, IssueState, Ref
from agent_pm.utils import one_line, parse_time


@dataclass
class Promotion:
    source: Approved
    detail: Detail
    role: str
    next: str
    cutoff: str | None
    first: str
    latest: str
    child: Ref | None = None


HELP = """Promote Handoff issues to their configured next role, then prune finished work.

Usage: agent-pm-linear promote [--dry-run] [--now]

Arguments and options:
  --dry-run   Print the plan; no board changes, session closes or file deletion.
              Still read Linear, configuration, local run/checkout inventory and
              Delegate status; requires access to those resources.
  --now       Skip the default 10-minute wait in Handoff for a manual run.
  -h, --help  Print this help alone; exit 0.
  Boolean flags may repeat; repeating them has no further effect.

Prerequisites and effects:
  Requires Python 3.11+, configured Linear/Delegate settings, Linear access and
  the harness_key Keychain service. Configuration: agent-pm-linear --help
  For each Handoff issue in a project, assigned to a configured role with next:
  create that next role's issue in the same project, assigned to its account,
  in Todo with source links and human instructions; relate the issues and move
  the source to Done. Existing next-stage issues are reused. Required human
  instructions absent: return the source to In Review with a comment.
  Every tick also prunes: close finished runs, delete agent files and archive
  finished issues (Done ones of pm/engineer only). This includes uncommitted
  agent work. Cleanup ages, safeguards and retention:
  agent-pm-linear prune --help
  A prune failure is logged without breaking Handoff work.
  Events: orchestrator.jsonl (src promote); an idle run writes none. Dry-run
  plans print JSON lines to stderr. Exit 0: tick completed; 1: config/Linear
  error (logged once a day); 2: bad arguments. Individual Handoff errors do
  not prevent other promotions; prolonged failures can return an issue to
  In Review with a comment.

Example (configured Linear access and Handoff issues):
  agent-pm-linear promote --dry-run --now
  Preview promotion and cleanup without waiting for Handoff maturity.

Recovery and next help:
  Review the plan before executing: agent-pm-linear promote
  Inspect logged Handoff errors and fix the reported configuration, credentials
  or missing human instructions before retrying.
  Cleanup details: agent-pm-linear prune --help
  Command discovery: agent-pm-linear --help
"""
GRACE = timedelta(hours=1)
MATURE = timedelta(minutes=10)
NO_INSTRUCTIONS = "Handoff needs a comment saying what to build next. Moving back to In Review."


def child_title(prefix, src_prefix, title):
    title = one_line(
        title
    )  # agent-written: must not break out of its line and pose as the human Instructions section
    if src_prefix and title.startswith(f"{src_prefix}: "):
        title = title[len(src_prefix) + 2 :]
    return f"{prefix}: {title}"


class Promoter:
    def __init__(self, gql, cfg, now, dry, *, tasks, wait=True):
        self.cfg, self.now, self.dry, self.wait = cfg, now, dry, wait
        self.tasks = tasks
        self.team = team(gql, cfg)
        self.roles = role_ids(gql, cfg.roles)
        self.tracker = LinearTracker(
            gql,
            states=self.team.states,
            team_id=self.team.id,
            finished=self.team.finished,
            roles=self.roles,
            humans=cfg.human_members,
        )
        self.say = functools.partial(log, "promote", dry=dry)

    def pending(self):
        work = []
        for src in self.tracker.approved(self.cfg.roles):
            role = src.owner
            nxt = self.cfg.roles[role].next
            if nxt:
                try:
                    detail = self.tracker.detail(src.key)
                    if detail.state is not IssueState.APPROVED:
                        continue
                    cutoff, first, latest = self.moves(src, detail)
                    work.append(Promotion(src, detail, role, nxt, cutoff, first, latest))
                except Unavailable:
                    raise
                except Exception as e:  # noqa: BLE001  # Keep reading the other handoffs.
                    self.say(
                        "handoff-error",
                        src.ident,
                        error=one_line(e),
                        traceback="".join(format_exception(e)),
                    )
        return sorted(work, key=lambda handoff: handoff.latest)

    def run(self):
        for handoff in self.pending():
            src, latest = handoff.source, handoff.latest
            if self.wait and self.now - parse_time(latest) < MATURE:
                self.say(
                    "handoff-wait",
                    src.ident,
                    once=True,
                    reason=f"in Handoff under {MATURE.seconds // 60} min",
                )
                continue
            try:
                self.promote(handoff)
            except Unavailable:
                raise
            except Exception as e:  # noqa: BLE001  # Keep processing other handoffs.
                self.say(
                    "handoff-error",
                    src.ident,
                    error=one_line(e),
                    traceback="".join(format_exception(e)),
                )
                if self.now - parse_time(latest) > GRACE:
                    self.bounce_failed(src, e, handoff.child)

    def moves(self, src, detail):
        hist = sorted(detail.moves, key=lambda m: parse_time(m.at))
        cutoff = max(
            (m.at for m in hist if m.to is IssueState.REVIEW and m.frm is not IssueState.APPROVED),
            key=parse_time,
            default=None,
        )
        handoffs = [
            m.at
            for m in hist
            if m.to is IssueState.APPROVED
            and (cutoff is None or parse_time(m.at) > parse_time(cutoff))
        ]
        first = handoffs[0] if handoffs else src.created
        return cutoff, first, handoffs[-1] if handoffs else first

    def instructions(self, detail, cutoff):
        return sorted(
            (
                c
                for c in detail.comments
                if c.other and (cutoff is None or parse_time(c.at) > parse_time(cutoff))
            ),
            key=lambda c: parse_time(c.at),
        )

    def promote(self, handoff: Promotion):
        src, detail, role, nxt = handoff.source, handoff.detail, handoff.role, handoff.next
        comments = self.instructions(detail, handoff.cutoff)
        if not comments and self.cfg.roles[role].require_instructions:
            if self.comment_and_move(src, NO_INSTRUCTIONS, "handoff-bounce"):
                self.say("handoff-bounce", src.ident, to="in_review", reason="no instructions")
            return
        once = (src.key, nxt, handoff.first)
        existing = self.tracker.created(once)
        if existing:
            child = handoff.child = existing
        elif self.dry:
            self.say("promote", src.ident, role=nxt, project=src.project_name)
            return
        else:
            child = handoff.child = self.tracker.create(
                once,
                project=src.project_key,
                owner=nxt,
                priority=src.priority,
                title=child_title(
                    self.tasks[nxt].prefix,
                    self.tasks[role].prefix or None,
                    src.title,
                ),
                description=self.description(src, comments, detail),
            )
        if child.key not in detail.related and not self.dry:
            self.tracker.relate(src.key, child.key)
        if not self.move(src, IssueState.DONE, "promote"):
            return
        self.say("promote", src.ident, to=child.ident)
        try:  # the source is Done now; a lost comment must not bounce it
            self.comment(src, f"Promoted to {child.ident}.")
        except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as e:
            self.say(
                "handoff-error",
                src.ident,
                reason="promoted, but the comment failed",
                error=one_line(e),
            )

    def description(self, src, comments, detail):
        everything = sorted(
            (c for c in detail.comments if not c.run), key=lambda c: parse_time(c.at)
        )
        return handoff_text(
            src.ident,
            src.url,
            sources=list(src.sources),
            instructions=[(c.author, c.at, c.body) for c in comments],
            comments=[(c.author, c.at, c.body) for c in everything],
        )

    def bounce_failed(self, src, error, child):
        body = f"Handoff failed: {str(error)[:300]}" + (
            f" The next-stage issue {child.ident} already exists." if child else ""
        )
        try:
            if self.comment_and_move(src, body, "handoff-failed"):
                self.say("handoff-failed", src.ident, to="in_review")
        except Unavailable:
            raise
        except (LinearError, OSError, subprocess.SubprocessError) as e:
            self.say(
                "handoff-error",
                src.ident,
                reason="could not move to In Review",
                error=one_line(e),
            )

    def comment(self, src, body):
        if not self.dry:
            self.tracker.comment(src.key, body)

    def move(self, src, state, step):
        return self.dry or self.moved(
            src, step, self.tracker.move(src.key, state, frm=IssueState.APPROVED)
        )

    def comment_and_move(self, src, body, step):
        return self.dry or self.moved(
            src,
            step,
            self.tracker.comment_and_move(
                src.key, body, to=IssueState.REVIEW, frm=IssueState.APPROVED, notify=True
            ),
        )

    def moved(self, src, step, left):
        if left is not None:
            self.say("move-skip", src.ident, step=step, state=left)
        return left is None


def run_prune(gql, now, promoter, pruner=None):
    try:
        if pruner is None:
            pruner = Pruner
        pruner(
            gql,
            now,
            dry=promoter.dry,
            work=promoter.cfg.workdirs,
            team=promoter.team,
            roles=promoter.roles,
            cfg=promoter.cfg,
        ).run()
    except Unavailable as e:
        linear_error("prune", e, dry=promoter.dry)
    except (Exception, SystemExit) as e:  # noqa: BLE001  # Prune failures leave promotions intact.
        log(
            "prune",
            "prune-error",
            dry=promoter.dry,
            error=one_line(e),
            traceback="".join(format_exception(e)),
        )


def main(argv, gql=linear_gql, now=None, config_path=None, pruner=None):
    if argv in (["--help"], ["-h"]):
        print(HELP.strip())
        return 0
    os.environ["PATH"] = config.path()
    if any(a not in ("--dry-run", "--now") for a in argv):
        print(HELP.strip(), file=sys.stderr)
        return 2
    dry = "--dry-run" in argv
    try:
        cfg = config.load(path=config_path)
    except ConfigError as error:
        config.report_error("promote", error, dry=dry)
        return 1
    event_log.use_logs(cfg.logs_dir)
    gql = functools.partial(gql, service=cfg.harness_key)
    now = now or datetime.now(UTC)
    try:
        promoter = Promoter(gql, cfg, now, dry, tasks=config.TASKS, wait="--now" not in argv)
        promoter.run()
    except LinearError as e:
        return event_log.config_error("promote", e, dry=dry)
    except Unavailable as e:
        return linear_error("promote", e, dry=dry)
    run_prune(gql, now, promoter, pruner)
    return 0
