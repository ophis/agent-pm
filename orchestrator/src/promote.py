#!/usr/bin/env python3
"""Promote issues in Handoff to the next role.

For each Handoff issue in a project, assigned to a role account whose [roles.<role>] in orchestrator/config.toml has a next:
create that role's issue in the same project, assigned to its account, in Todo with the source links and the
human instructions, relate it, and move the source to Done.
--dry-run   Change nothing; print what would happen.
--now       Skip the 10-minute wait in Handoff (for a manual run).
Needs Python 3.11+ (tomllib).

At the end of every tick, orchestrator/src/prune.py's Pruner removes the checkouts of
finished issues (TASK-49), closes their left-open TUI sessions and archives finished pm and engineer issues; a prune
failure is logged and never breaks the Handoff work.
"""
import hashlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CONFIG, PATH, TASKS, load_config, runnable  # noqa: E402
import linear  # noqa: E402
from linear import linear_gql, one_line, parse_time, role_ids, stamp, team  # noqa: E402
import sessions  # noqa: E402

GRACE = timedelta(hours=1)
MATURE = timedelta(minutes=10)  # undo window for an accidental drag into Handoff
NO_INSTRUCTIONS = "Handoff needs a comment saying what to build next. Moving back to In Review."

Q_HANDOFF = """query($t: ID, $s: ID, $a: [ID!]) { issues(filter: { team: { id: { eq: $t } }, state: { id: { eq: $s } },
  project: { null: false }, assignee: { id: { in: $a } } }, first: 100) {
  nodes { id identifier url title priority createdAt project { id name } assignee { id } attachments { nodes { title url } } } } }"""
Q_DETAIL = """query($i: String!) { issue(id: $i) { state { id }
  """ + linear.HISTORY + """
  comments(first: 250) { nodes { body createdAt user { email name isMe } } }
  relations(first: 250) { nodes { relatedIssue { id } } }
  inverseRelations(first: 250) { nodes { issue { id } } } } }"""
Q_CHILD = """query($c: ID!) { issues(filter: { id: { eq: $c } }, includeArchived: true) { nodes { id identifier } } }"""
M_CREATE = "mutation($in: IssueCreateInput!) { issueCreate(input: $in) { success issue { id identifier } } }"
M_RELATE = "mutation($in: IssueRelationCreateInput!) { issueRelationCreate(input: $in) { success } }"


def child_id(source_id, target, handoff_at):
    key = f"{source_id}/{target}/{handoff_at}".encode()
    return str(uuid.UUID(bytes=hashlib.sha256(key).digest()[:16], version=4))


def child_title(prefix, src_prefix, title):
    title = one_line(title)  # agent-written: must not break out of its line and pose as the human Instructions section
    if src_prefix and title.startswith(f"{src_prefix}: "):
        title = title[len(src_prefix) + 2:]
    return f"{prefix}: {title}"


class Promoter:
    def __init__(self, gql, cfg, now, dry, wait=True):
        self.gql, self.cfg, self.now, self.dry, self.wait = gql, cfg, now, dry, wait
        self.team = team(gql, cfg)
        self.states = self.team.states
        self.humans = {e.lower() for e in cfg.get("human_members") or []}
        self.runs = runnable(cfg)
        self.roles = role_ids(gql, self.runs)
        self.ids = {r: i for i, r in self.roles.items()}

    def say(self, msg):
        self.said = True
        print(f"{stamp()} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def run(self):
        self.said = False
        issues = self.gql(Q_HANDOFF, t=self.team.id, s=self.states["handoff"], a=list(self.roles))["issues"]["nodes"]
        work = []
        for src in issues:
            role = self.roles[src["assignee"]["id"]]
            nxt = self.cfg["roles"].get(role, {}).get("next")
            if nxt:
                try:
                    detail = self.gql(Q_DETAIL, i=src["id"])["issue"]
                    if detail["state"]["id"] != self.states["handoff"]:  # the Handoff list can lag behind a just-made move
                        continue
                    work.append((self.moves(src, detail), src, detail, role, nxt))
                except (Exception, SystemExit) as e:
                    self.say(f"handoff-error {src['identifier']}: {e}")
        for (cutoff, first, latest), src, detail, role, nxt in sorted(work, key=lambda w: w[0][2]):
            if self.wait and self.now - parse_time(latest) < MATURE:
                self.say(f"handoff-wait {src['identifier']} (in Handoff under {MATURE.seconds // 60} min)")
                continue
            found = [None]
            try:
                self.promote(src, detail, role, nxt, cutoff, first, found)
            except (Exception, SystemExit) as e:  # linear_gql raises SystemExit on API errors
                self.say(f"handoff-error {src['identifier']}: {e}")
                if self.now - parse_time(latest) > GRACE:
                    self.bounce_failed(src, e, found[0])
        if not self.said:  # one line per run, so the log shows the job is alive
            self.say(f"promote: nothing to do ({len(issues)} in Handoff)")

    def moves(self, src, detail):
        """(cutoff, first Handoff move after it, latest Handoff move), as API timestamps."""
        hist = sorted(detail["history"]["nodes"], key=lambda h: parse_time(h["createdAt"]))
        cutoff = max((h["createdAt"] for h in hist if h["toStateId"] == self.states["in_review"]
                      and h["fromStateId"] != self.states["handoff"]), key=parse_time, default=None)
        handoffs = [h["createdAt"] for h in hist if h["toStateId"] == self.states["handoff"]
                    and (cutoff is None or parse_time(h["createdAt"]) > parse_time(cutoff))]
        first = handoffs[0] if handoffs else src["createdAt"]
        return cutoff, first, handoffs[-1] if handoffs else first

    def instructions(self, detail, cutoff):
        """Comments after the cutoff by `human_members` users."""
        return sorted((c for c in detail["comments"]["nodes"] if c["user"] and (c["user"].get("email") or "").lower() in self.humans
                       and (cutoff is None or parse_time(c["createdAt"]) > parse_time(cutoff))),
                      key=lambda c: parse_time(c["createdAt"]))

    def promote(self, src, detail, role, nxt, cutoff, first, found):
        comments = self.instructions(detail, cutoff)
        required = self.cfg["roles"].get(role, {}).get("require_instructions", True)
        if not comments and required:
            if self.comment_and_move(src, NO_INSTRUCTIONS, "handoff-bounce"):
                self.say(f"handoff-bounce {src['identifier']} no instructions")
            return
        cid = child_id(src["id"], nxt, first)
        existing = self.gql(Q_CHILD, c=cid)["issues"]["nodes"]
        if existing:
            child = found[0] = existing[0]
        elif self.dry:
            self.say(f"promote {src['identifier']} -> new {nxt} issue in {src['project']['name']}")
            return
        else:
            child = found[0] = linear.call(self.gql, M_CREATE, "issueCreate", **{"in": {
                "id": cid, "teamId": self.team.id, "projectId": src["project"]["id"], "assigneeId": self.ids[nxt],
                "stateId": self.states["todo"], "priority": src["priority"],
                "title": child_title(TASKS[self.runs[nxt].default].prefix, TASKS[self.runs[role].default].prefix or None, src["title"]),
                "description": self.description(src, comments, detail)}})["issue"]
        related = {r["relatedIssue"]["id"] for r in detail["relations"]["nodes"]}
        related |= {r["issue"]["id"] for r in detail["inverseRelations"]["nodes"]}
        if child["id"] not in related and not self.dry:
            linear.call(self.gql, M_RELATE, "issueRelationCreate",
                        **{"in": {"type": "related", "issueId": src["id"], "relatedIssueId": child["id"]}})
        if not self.move(src, "done", "promote"):
            return
        self.say(f"promote {src['identifier']} -> {child['identifier']}")
        try:  # the source is Done now; a lost comment must not bounce it
            self.comment(src, f"Promoted to {child['identifier']}.")
        except (Exception, SystemExit) as e:
            self.say(f"handoff-error {src['identifier']}: promoted, but the comment failed: {e}")

    def description(self, src, comments, detail):
        parts = [f"Handoff from {src['identifier']}: {src['url']}"]
        attachments = [a for a in src["attachments"]["nodes"] if not sessions.is_record(a)]
        if attachments:
            parts.append("## Source\n" + "\n".join(f"- {one_line(a['title'])}: {one_line(a['url'])}" for a in attachments))
        if comments:
            parts.append("## Instructions\n" + "\n\n".join(
                f"{c['user']['name']}, {c['createdAt']}:\n{c['body']}" for c in comments))
        everything = sorted((c for c in detail["comments"]["nodes"] if not sessions.is_comment(c)),
                            key=lambda c: parse_time(c["createdAt"]))
        if everything:
            # Quoted so agent-written text cannot pose as a section of this description.
            parts.append("## Comments\n" + "\n".join(
                f"- {(c['user'] or {}).get('name', 'integration')}, {c['createdAt']}:\n"
                + "\n".join(f"  > {line}" for line in c["body"].splitlines()) for c in everything))
        return "\n\n".join(parts)

    def bounce_failed(self, src, error, child):
        body = f"Handoff failed: {str(error)[:300]}" + (f" The next-stage issue {child['identifier']} already exists." if child else "")
        try:
            if self.comment_and_move(src, body, "handoff-failed"):
                self.say(f"handoff-failed {src['identifier']} moved to In Review")
        except (Exception, SystemExit) as e:
            self.say(f"handoff-error {src['identifier']}: could not move to In Review: {e}")

    def comment(self, src, body):
        if not self.dry:
            linear.comment(self.gql, src["id"], body)

    def move(self, src, state, prefix):
        """linear.move from Handoff; True once moved (or dry)."""
        return self.dry or self.moved(src, prefix, linear.move(self.gql, src["id"], self.states[state], self.states["handoff"]))

    def comment_and_move(self, src, body, prefix):
        """linear.comment_and_move from Handoff to In Review, the humans subscribed; True once moved (or dry)."""
        return self.dry or self.moved(src, prefix, linear.comment_and_move(
            self.gql, src["id"], body, self.states["in_review"], self.states["handoff"], self.cfg.get("human_members") or []))

    def moved(self, src, prefix, left):
        """True for a move made (left None); else logs the skip."""
        if left is not None:
            self.say(f"{prefix} {src['identifier']}: issue is {left}")
        return left is None


def run_prune(gql, now, dry, team, roles, pruner=None):
    """Prune finished issues' checkouts and archive finished pm and engineer issues.
    A prune failure, even an ImportError, is logged and never breaks promote."""
    try:
        if pruner is None:
            from prune import Pruner as pruner
        pruner(gql, now, dry, team=team, roles=roles).run()
    except (Exception, SystemExit) as e:  # linear_gql raises SystemExit on API errors
        print(f"{stamp()} prune-error: {e}", flush=True)


def main(argv, gql=linear_gql, now=None, config=CONFIG, pruner=None):
    os.environ["PATH"] = PATH
    if any(a not in ("--dry-run", "--now") for a in argv):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    cfg = load_config(config)
    now = now or datetime.now(timezone.utc)
    dry = "--dry-run" in argv
    promoter = Promoter(gql, cfg, now, dry, wait="--now" not in argv)
    promoter.run()
    run_prune(gql, now, dry, promoter.team, promoter.roles, pruner)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
