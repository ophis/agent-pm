#!/usr/bin/env python3
import functools
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from traceback import format_exception
from typing import Any

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import ConfigError, LinearError, Unavailable
from agent_pm.linear_orchestrator.linear import issues, linear, sessions
from agent_pm.linear_orchestrator.linear.linear import (
    linear_error,
    linear_gql,
    log,
    one_line,
    parse_time,
    role_ids,
    team,
)


@dataclass
class Promotion:
    source: dict[str, Any]
    detail: dict[str, Any]
    role: str
    next: str
    cutoff: str | None
    first: str
    latest: str
    child: dict[str, Any] | None = None


HELP = """Promote issues in Handoff to the next role.

For each Handoff issue in a project, assigned to a role account whose [roles.<role>] in linear-orchestrator/config.toml has a next:
create that role's issue in the same project, assigned to its account, in Todo with the source links and the
human instructions, relate it, and move the source to Done.
--dry-run   Change nothing; print what would happen.
--now       Skip the 10-minute wait in Handoff (for a manual run).
Needs Python 3.11+ (tomllib). Events go to orchestrator.jsonl (linear.log, src promote); an idle run writes none; a
config error or Linear unavailable exits 1, logged once a day (linear.config_error, linear.linear_error).

At the end of every tick, prune.py's Pruner removes the worktrees and clones of finished issues (TASK-49), closes
their left-open TUI sessions and archives finished pm and engineer issues; a prune failure is logged and never breaks
the Handoff work.
"""
GRACE = timedelta(hours=1)
MATURE = timedelta(minutes=10)
NO_INSTRUCTIONS = "Handoff needs a comment saying what to build next. Moving back to In Review."

Q_HANDOFF = """query($t: ID, $s: ID, $a: [ID!]) { issues(filter: { team: { id: { eq: $t } }, state: { id: { eq: $s } },
  project: { null: false }, assignee: { id: { in: $a } } }, first: 100) {
  nodes { id identifier url title priority createdAt project { id name } assignee { id } attachments { nodes { title url } } } } }"""
Q_DETAIL = (
    """query($i: String!) { issue(id: $i) { state { id }
  """
    + linear.HISTORY
    + """
  comments(first: 250) { nodes { body createdAt user { email name isMe } } }
  relations(first: 250) { nodes { relatedIssue { id } } }
  inverseRelations(first: 250) { nodes { issue { id } } } } }"""
)
Q_CHILD = """query($c: ID!) { issues(filter: { id: { eq: $c } }, includeArchived: true) { nodes { id identifier } } }"""
M_CREATE = "mutation($in: IssueCreateInput!) { issueCreate(input: $in) { success issue { id identifier } } }"
M_RELATE = (
    "mutation($in: IssueRelationCreateInput!) { issueRelationCreate(input: $in) { success } }"
)


def child_id(source_id, target, handoff_at):
    return linear.client_id(source_id, target, handoff_at)


def child_title(prefix, src_prefix, title):
    title = one_line(
        title
    )  # agent-written: must not break out of its line and pose as the human Instructions section
    if src_prefix and title.startswith(f"{src_prefix}: "):
        title = title[len(src_prefix) + 2 :]
    return f"{prefix}: {title}"


class Promoter:
    def __init__(self, gql, cfg, now, dry, *, tasks, wait=True):
        self.gql, self.cfg, self.now, self.dry, self.wait = gql, cfg, now, dry, wait
        self.tasks = tasks
        self.team = team(gql, cfg)
        self.states = self.team.states
        self.humans = {e.lower() for e in cfg.human_members}
        self.roles = role_ids(gql, cfg.roles)
        self.ids = {r: i for i, r in self.roles.items()}
        self.say = functools.partial(log, "promote", dry=dry)

    def pending(self):
        issues = self.gql(Q_HANDOFF, t=self.team.id, s=self.states["handoff"], a=list(self.roles))[
            "issues"
        ]["nodes"]
        work = []
        for src in issues:
            role = self.roles[src["assignee"]["id"]]
            nxt = self.cfg.roles[role].next
            if nxt:
                try:
                    detail = self.gql(Q_DETAIL, i=src["id"])["issue"]
                    if detail["state"]["id"] != self.states["handoff"]:
                        continue
                    cutoff, first, latest = self.moves(src, detail)
                    work.append(Promotion(src, detail, role, nxt, cutoff, first, latest))
                except Unavailable:
                    raise
                except Exception as e:  # noqa: BLE001  # Keep reading the other handoffs.
                    self.say(
                        "handoff-error",
                        src["identifier"],
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
                    src["identifier"],
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
                    src["identifier"],
                    error=one_line(e),
                    traceback="".join(format_exception(e)),
                )
                if self.now - parse_time(latest) > GRACE:
                    self.bounce_failed(src, e, handoff.child)

    def moves(self, src, detail):
        hist = sorted(detail["history"]["nodes"], key=lambda h: parse_time(h["createdAt"]))
        cutoff = max(
            (
                h["createdAt"]
                for h in hist
                if h["toStateId"] == self.states["in_review"]
                and h["fromStateId"] != self.states["handoff"]
            ),
            key=parse_time,
            default=None,
        )
        handoffs = [
            h["createdAt"]
            for h in hist
            if h["toStateId"] == self.states["handoff"]
            and (cutoff is None or parse_time(h["createdAt"]) > parse_time(cutoff))
        ]
        first = handoffs[0] if handoffs else src["createdAt"]
        return cutoff, first, handoffs[-1] if handoffs else first

    def instructions(self, detail, cutoff):
        return sorted(
            (
                c
                for c in detail["comments"]["nodes"]
                if c["user"]
                and (c["user"].get("email") or "").lower() in self.humans
                and (cutoff is None or parse_time(c["createdAt"]) > parse_time(cutoff))
            ),
            key=lambda c: parse_time(c["createdAt"]),
        )

    def promote(self, handoff: Promotion):
        src, detail, role, nxt = handoff.source, handoff.detail, handoff.role, handoff.next
        comments = self.instructions(detail, handoff.cutoff)
        if not comments and self.cfg.roles[role].require_instructions:
            if self.comment_and_move(src, NO_INSTRUCTIONS, "handoff-bounce"):
                self.say(
                    "handoff-bounce", src["identifier"], to="in_review", reason="no instructions"
                )
            return
        cid = child_id(src["id"], nxt, handoff.first)
        existing = self.gql(Q_CHILD, c=cid)["issues"]["nodes"]
        if existing:
            child = handoff.child = existing[0]
        elif self.dry:
            self.say("promote", src["identifier"], role=nxt, project=src["project"]["name"])
            return
        else:
            child = handoff.child = linear.call(
                self.gql,
                M_CREATE,
                "issueCreate",
                **{
                    "in": {
                        "id": cid,
                        "teamId": self.team.id,
                        "projectId": src["project"]["id"],
                        "assigneeId": self.ids[nxt],
                        "stateId": self.states["todo"],
                        "priority": src["priority"],
                        "title": child_title(
                            self.tasks[nxt].prefix,
                            self.tasks[role].prefix or None,
                            src["title"],
                        ),
                        "description": self.description(src, comments, detail),
                    }
                },
            )["issue"]
        related = {r["relatedIssue"]["id"] for r in detail["relations"]["nodes"]}
        related |= {r["issue"]["id"] for r in detail["inverseRelations"]["nodes"]}
        if child["id"] not in related and not self.dry:
            linear.call(
                self.gql,
                M_RELATE,
                "issueRelationCreate",
                **{"in": {"type": "related", "issueId": src["id"], "relatedIssueId": child["id"]}},
            )
        if not self.move(src, "done", "promote"):
            return
        self.say("promote", src["identifier"], to=child["identifier"])
        try:  # the source is Done now; a lost comment must not bounce it
            self.comment(src, f"Promoted to {child['identifier']}.")
        except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as e:
            self.say(
                "handoff-error",
                src["identifier"],
                reason="promoted, but the comment failed",
                error=one_line(e),
            )

    def description(self, src, comments, detail):
        everything = sorted(
            (c for c in detail["comments"]["nodes"] if not sessions.is_comment(c)),
            key=lambda c: parse_time(c["createdAt"]),
        )
        return issues.handoff(
            src["identifier"],
            src["url"],
            sources=[
                (a["title"], a["url"])
                for a in src["attachments"]["nodes"]
                if not sessions.is_record(a)
            ],
            instructions=[(c["user"]["name"], c["createdAt"], c["body"]) for c in comments],
            comments=[
                ((c["user"] or {}).get("name", "integration"), c["createdAt"], c["body"])
                for c in everything
            ],
        )

    def bounce_failed(self, src, error, child):
        body = f"Handoff failed: {str(error)[:300]}" + (
            f" The next-stage issue {child['identifier']} already exists." if child else ""
        )
        try:
            if self.comment_and_move(src, body, "handoff-failed"):
                self.say("handoff-failed", src["identifier"], to="in_review")
        except Unavailable:
            raise
        except (LinearError, OSError, subprocess.SubprocessError) as e:
            self.say(
                "handoff-error",
                src["identifier"],
                reason="could not move to In Review",
                error=one_line(e),
            )

    def comment(self, src, body):
        if not self.dry:
            linear.comment(self.gql, src["id"], body)

    def move(self, src, state, step):
        return self.dry or self.moved(
            src, step, linear.move(self.gql, src["id"], self.states[state], self.states["handoff"])
        )

    def comment_and_move(self, src, body, step):
        return self.dry or self.moved(
            src,
            step,
            linear.comment_and_move(
                self.gql,
                src["id"],
                body,
                to=self.states["in_review"],
                frm=self.states["handoff"],
                emails=self.cfg.human_members,
            ),
        )

    def moved(self, src, step, left):
        if left is not None:
            self.say("move-skip", src["identifier"], step=step, state=left)
        return left is None


def run_prune(gql, now, promoter, pruner=None):
    # a failure, even an ImportError, is logged
    try:
        if pruner is None:
            from agent_pm.linear_orchestrator.engine.prune import Pruner as pruner
        pruner(
            gql,
            now,
            dry=promoter.dry,
            work=promoter.cfg.runs_dir,
            team=promoter.team,
            roles=promoter.roles,
            driver=promoter.cfg.driver,
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


def main(argv, gql=linear_gql, now=None, config_path=config.CONFIG, pruner=None):
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
    linear.use_logs(cfg.logs_dir)
    gql = functools.partial(gql, service=cfg.harness_key)
    now = now or datetime.now(UTC)
    try:
        promoter = Promoter(gql, cfg, now, dry, tasks=config.TASKS, wait="--now" not in argv)
        promoter.run()
    except LinearError as e:
        return linear.config_error("promote", e, dry=dry)
    except Unavailable as e:
        return linear_error("promote", e, dry=dry)
    run_prune(gql, now, promoter, pruner)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
