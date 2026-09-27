#!/usr/bin/env python3
"""Promote issues in Handoff to the next stage (docs/specs/2026-09-27-promote-design.md).

For each issue in Handoff whose project has a `next` in pipeline.toml: create the next stage's issue
in Todo with the source links and the human instructions, relate it, and move the source to Done.
--dry-run   Change nothing; print what would happen.
Needs Python 3.11+ (tomllib).
"""
import hashlib
import os
import sys
import tomllib
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pick import linear_gql, parse_time  # noqa: E402

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline.toml")
GRACE = timedelta(hours=1)
NO_INSTRUCTIONS = "Handoff needs a comment saying what to build next. Moving back to In Review."
STATES = ("Todo", "In Review", "Handoff", "Done")

Q_SETUP = """query($t: String!) {
  teams(filter: { name: { eq: $t } }) { nodes { id projects(first: 50) { nodes { id name } } } }
  workflowStates(filter: { team: { name: { eq: $t } } }) { nodes { id name } } }"""
Q_HANDOFF = """query($t: String!) { issues(filter: { team: { name: { eq: $t } }, state: { name: { eq: "Handoff" } } }, first: 100) {
  nodes { id identifier url title priority createdAt project { id name } attachments { nodes { title url } } } } }"""
Q_DETAIL = """query($i: String!) { issue(id: $i) { state { name }
  history(first: 250) { nodes { createdAt fromStateId toStateId } }
  comments(first: 250) { nodes { body createdAt user { email name } } }
  relations(first: 250) { nodes { relatedIssue { id } } }
  inverseRelations(first: 250) { nodes { issue { id } } } } }"""
Q_CHILD = """query($c: ID!) { issues(filter: { id: { eq: $c } }, includeArchived: true) { nodes { id identifier } } }"""
M_CREATE = "mutation($in: IssueCreateInput!) { issueCreate(input: $in) { success issue { id identifier } } }"
M_RELATE = "mutation($in: IssueRelationCreateInput!) { issueRelationCreate(input: $in) { success } }"
M_COMMENT = "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }"
M_STATE = "mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }"


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    projects = cfg.get("projects", {})
    for name, p in projects.items():
        nxt = p.get("next")
        if nxt and "prefix" not in projects.get(nxt, {}):
            raise SystemExit(f"pipeline.toml: next of {name!r} must name a [projects] entry with a prefix")
    return cfg


def child_id(source_id, project_id, handoff_at):
    key = f"{source_id}/{project_id}/{handoff_at}".encode()
    return str(uuid.UUID(bytes=hashlib.sha256(key).digest()[:16], version=4))


def one_line(s):
    # Agent-written titles must not break out of their line and pose as the human Instructions section.
    return " ".join(s.split())


def ok(result, name):
    if not result[name]["success"]:
        raise RuntimeError(f"{name} returned success: false")
    return result[name]


class Promoter:
    def __init__(self, gql, cfg, now, dry):
        self.gql, self.cfg, self.now, self.dry = gql, cfg, now, dry
        setup = gql(Q_SETUP, t=cfg["team"])
        team = setup["teams"]["nodes"][0]
        self.team = team["id"]
        self.states = {s["name"]: s["id"] for s in setup["workflowStates"]["nodes"]}
        self.projects = {p["name"]: p["id"] for p in team["projects"]["nodes"]}
        missing = [s for s in STATES if s not in self.states]
        missing += [p["next"] for p in cfg.get("projects", {}).values() if p.get("next") and p["next"] not in self.projects]
        if missing:
            raise SystemExit(f"not found in Linear: {', '.join(missing)}")

    def say(self, msg):
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {'dry-run: ' if self.dry else ''}{msg}", flush=True)

    def run(self):
        issues = self.gql(Q_HANDOFF, t=self.cfg["team"])["issues"]["nodes"]
        work = []
        for src in issues:
            nxt = self.cfg.get("projects", {}).get((src["project"] or {}).get("name"), {}).get("next")
            if nxt:
                try:
                    detail = self.gql(Q_DETAIL, i=src["id"])["issue"]
                    if detail["state"]["name"] != "Handoff":  # the Handoff list can lag behind a just-made move
                        continue
                    work.append((self.moves(src, detail), src, detail, nxt))
                except (Exception, SystemExit) as e:
                    self.say(f"handoff-error {src['identifier']}: {e}")
        for (cutoff, first, latest), src, detail, nxt in sorted(work, key=lambda w: w[0][2]):
            found = [None]
            try:
                self.promote(src, detail, nxt, cutoff, first, found)
            except (Exception, SystemExit) as e:  # linear_gql raises SystemExit on API errors
                self.say(f"handoff-error {src['identifier']}: {e}")
                if self.now - parse_time(latest) > GRACE:
                    self.bounce_failed(src, e, found[0])

    def moves(self, src, detail):
        """(cutoff, first Handoff move after it, latest Handoff move), as API timestamps."""
        hist = sorted(detail["history"]["nodes"], key=lambda h: parse_time(h["createdAt"]))
        cutoff = max((h["createdAt"] for h in hist if h["toStateId"] == self.states["In Review"]
                      and h["fromStateId"] != self.states["Handoff"]), key=parse_time, default=None)
        handoffs = [h["createdAt"] for h in hist if h["toStateId"] == self.states["Handoff"]
                    and (cutoff is None or parse_time(h["createdAt"]) > parse_time(cutoff))]
        first = handoffs[0] if handoffs else src["createdAt"]
        return cutoff, first, handoffs[-1] if handoffs else first

    def instructions(self, detail, cutoff):
        members = {m.lower() for m in self.cfg["human_members"]}
        return sorted((c for c in detail["comments"]["nodes"] if c["user"] and c["user"]["email"].lower() in members
                       and (cutoff is None or parse_time(c["createdAt"]) > parse_time(cutoff))),
                      key=lambda c: parse_time(c["createdAt"]))

    def promote(self, src, detail, nxt, cutoff, first, found):
        comments = self.instructions(detail, cutoff)
        if not comments:
            self.comment_and_move(src, NO_INSTRUCTIONS, "In Review")
            self.say(f"handoff-bounce {src['identifier']} no instructions")
            return
        cid = child_id(src["id"], self.projects[nxt], first)
        existing = self.gql(Q_CHILD, c=cid)["issues"]["nodes"]
        if existing:
            child = found[0] = existing[0]
        elif self.dry:
            self.say(f"promote {src['identifier']} -> new {nxt} issue")
            return
        else:
            child = found[0] = ok(self.gql(M_CREATE, **{"in": {
                "id": cid, "teamId": self.team, "projectId": self.projects[nxt], "stateId": self.states["Todo"],
                "priority": src["priority"], "title": f"{self.cfg['projects'][nxt]['prefix']}: {one_line(src['title'])}",
                "description": self.description(src, comments)}}), "issueCreate")["issue"]
        related = {r["relatedIssue"]["id"] for r in detail["relations"]["nodes"]}
        related |= {r["issue"]["id"] for r in detail["inverseRelations"]["nodes"]}
        if child["id"] not in related and not self.dry:
            ok(self.gql(M_RELATE, **{"in": {"type": "related", "issueId": src["id"], "relatedIssueId": child["id"]}}),
               "issueRelationCreate")
        self.move(src, "Done")
        self.say(f"promote {src['identifier']} -> {child['identifier']}")
        try:  # the source is Done now; a lost comment must not bounce it
            self.comment(src, f"Promoted to {child['identifier']}.")
        except (Exception, SystemExit) as e:
            self.say(f"handoff-error {src['identifier']}: promoted, but the comment failed: {e}")

    def description(self, src, comments):
        parts = [f"Handoff from {src['identifier']}: {src['url']}"]
        attachments = src["attachments"]["nodes"]
        if attachments:
            parts.append("## Source\n" + "\n".join(f"- {one_line(a['title'])}: {one_line(a['url'])}" for a in attachments))
        parts.append("## Instructions\n" + "\n\n".join(
            f"{c['user']['name']}, {c['createdAt']}:\n{c['body']}" for c in comments))
        return "\n\n".join(parts)

    def bounce_failed(self, src, error, child):
        body = f"Handoff failed: {str(error)[:300]}" + (f" The next-stage issue {child['identifier']} already exists." if child else "")
        try:
            self.comment_and_move(src, body, "In Review")
            self.say(f"handoff-failed {src['identifier']} moved to In Review")
        except (Exception, SystemExit) as e:
            self.say(f"handoff-error {src['identifier']}: could not move to In Review: {e}")

    def comment(self, src, body):
        if not self.dry:
            ok(self.gql(M_COMMENT, i=src["id"], b=body), "commentCreate")

    def move(self, src, state):
        if not self.dry:
            ok(self.gql(M_STATE, i=src["id"], s=self.states[state]), "issueUpdate")

    def comment_and_move(self, src, body, state):
        # Move first: if the move fails, no comment is posted, so retries don't repeat it.
        self.move(src, state)
        self.comment(src, body)


def main(argv, gql=linear_gql, now=None, config=CONFIG):
    if any(a != "--dry-run" for a in argv):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    cfg = load_config(config)
    Promoter(gql, cfg, now or datetime.now(timezone.utc), "--dry-run" in argv).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
