#!/usr/bin/env python3
"""Plan, Recover, Pick and Claim for the Deep Research queue, via the Linear API.

--plan [RUNS_LOG]   Recover, then print "resume <ID> <SID> <k> <url>", "new", or nothing.
--prune RUNS_LOG    Drop runs.log lines older than 7 days.
--claim [RUNS_LOG]  Pick + Claim: print "<ID> <url>" of the claimed issue, or nothing.
no mode [RUNS_LOG]  Recover, then Pick + Claim (manual use).
--gate resume|new   Read the usage probe's stream-json on stdin, print the usage, exit 0 if the run may start.
--dry-run           Change nothing.
The API key is read from the macOS Keychain (service linear-api-key, account frank.agent.w).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone

TEAM = "Frank's Agents"
PROJECT = "Deep Research"
STALE = timedelta(hours=2)
LIVE = timedelta(minutes=30)
CAP = 4
KEEP = timedelta(days=7)
SKEW = timedelta(minutes=5)
MAX_5H = 0.9
CAP_COMMENT = "Tried 4 times without finishing; needs a look."
INTERRUPTED = "The previous research run was interrupted. Moving this issue back to the Todo queue."
USAGE = "usage: pick.py [--plan | --claim] [--dry-run] [RUNS_LOG] | --gate resume|new | --prune RUNS_LOG"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_LOG = os.path.join(ROOT, "logs", "runs.log")
# claude keys transcripts by cwd, with "/" and "." replaced by "-".
TRANSCRIPTS = os.path.expanduser("~/.claude/projects/" + os.path.join(ROOT, "work").replace("/", "-").replace(".", "-"))
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\b")
LINE = re.compile(TS.pattern + r" (start|resume) (\S+) session=(\S+)")


def linear_gql(query, **variables):
    key = subprocess.run(["security", "find-generic-password", "-a", "frank.agent.w", "-s", "linear-api-key", "-w"],
                         capture_output=True, text=True, check=True).stdout.strip()
    req = urllib.request.Request("https://api.linear.app/graphql",
                                 data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"linear api error: {body['errors']}")
    return body["data"]


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr)


def local_time(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)


def parse_log(path):
    """(time, kind, issue, sid) of every start/resume line, in file order. runs.log timestamps are local time."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    entries = []
    for line in lines:
        m = LINE.match(line)
        if m:
            entries.append((local_time(m[1]), m[2], m[3], m[4]))
    return entries


def prune(path, now):
    """Drop lines older than KEEP; an untimestamped line takes the time of the nearest earlier timestamped one."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    keep, ts = [], None
    for line in lines:
        m = TS.match(line)
        if m:
            ts = local_time(m[1])
        if ts and ts >= now - KEEP:
            keep.append(line)
    if len(keep) == len(lines):
        return
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".runs.log.")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(keep)
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def latest_sid(entries, issue):
    return next((e[3] for e in reversed(entries) if e[2] == issue), None)


def sid_times(entries, sid):
    return [e[0] for e in entries if e[3] == sid]


def first_line_time(entries, sid):
    return next((e[0] for e in entries if e[3] == sid and e[1] == "start"), sid_times(entries, sid)[0])


def resume_count(entries, sid):
    return sum(1 for e in entries if e[1] == "resume" and e[3] == sid)


def attempt_count(entries, issue, since=None):
    return sum(1 for e in entries if e[2] == issue and (since is None or e[0] > since))


def is_live(tdir, sid, now):
    cutoff = (now - LIVE).timestamp()
    paths = [os.path.join(tdir, f"{sid}.jsonl")]
    for root, _, files in os.walk(os.path.join(tdir, sid)):
        paths += [os.path.join(root, f) for f in files]
    for p in paths:
        try:
            if os.path.getmtime(p) > cutoff:
                return True
        except OSError:
            pass
    return False


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def rank(issue):
    return issue["priority"] or 5  # 0 = no priority = lowest


def gate(kind, lines):
    """(ok, summary) from the last rate_limit_event of the probe's stream-json."""
    info = None
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict) and m.get("type") == "rate_limit_event":
            info = m.get("rate_limit_info") or {}
    if info is None:
        return False, "no rate_limit_event"
    windows = info.get("unifiedWindows") or {}
    five = (windows.get("five_hour") or {}).get("utilization")
    week = {k: v.get("utilization") for k, v in windows.items() if k.startswith("seven_day") and isinstance(v, dict)}
    summary = " ".join([f"status={info.get('status')}", f"five_hour={five}"] + [f"{k}={v}" for k, v in sorted(week.items())])
    if five is None:
        return False, summary
    ok = info.get("status") != "rejected" and five < MAX_5H and all(v is None or v < 1 for v in week.values())
    return ok, summary


class Board:
    def __init__(self, gql, entries, tdir, now, dry):
        self.gql, self.entries, self.tdir, self.now, self.dry = gql, entries, tdir, now, dry
        self.hist = {}
        self.me = gql("{ viewer { id } }")["viewer"]["id"]
        self.states = {s["name"]: s["id"] for s in gql("""query($t: String!) {
            workflowStates(filter: { team: { name: { eq: $t } } }) { nodes { id name } } }""", t=TEAM)["workflowStates"]["nodes"]}

    def issues(self, state, extra=None):
        flt = {"project": {"name": {"eq": PROJECT}}, "state": {"name": {"eq": state}}, **(extra or {})}
        return self.gql("""query($f: IssueFilter) { issues(filter: $f, first: 100) {
                    nodes { id identifier url priority createdAt updatedAt state { name } } } }""", f=flt)["issues"]["nodes"]

    def last_move(self, issue, state, by_user=False):
        """Latest time the issue was moved to state (by_user: by someone other than the agent)."""
        if issue["id"] not in self.hist:
            # orderBy createdAt returns newest first, so the latest moves are on this page.
            self.hist[issue["id"]] = self.gql("""query($i: String!) { issue(id: $i) { history(first: 250, orderBy: createdAt) {
                    nodes { createdAt actorId toStateId } } } }""", i=issue["id"])["issue"]["history"]["nodes"]
        times = [parse_time(n["createdAt"]) for n in self.hist[issue["id"]] if n["toStateId"] == self.states[state]
                 and (not by_user or (n["actorId"] and n["actorId"] != self.me))]
        return max(times, default=None)

    def attempts(self, issue):
        n = attempt_count(self.entries, issue["identifier"])
        return n if n < CAP else attempt_count(self.entries, issue["identifier"], self.last_move(issue, "Todo", by_user=True))

    def current_sid(self, issue):
        """The issue's latest SID, unless it began before the issue's latest move to In Progress (minus SKEW)."""
        sid = latest_sid(self.entries, issue["identifier"])
        if not sid:
            return None
        moved = self.last_move(issue, "In Progress")
        return sid if moved is None or first_line_time(self.entries, sid) >= moved - SKEW else None

    def comment_and_move(self, issue, body, state, **extra):
        if self.dry:
            return
        self.gql("mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }",
                 i=issue["id"], b=body)
        self.gql("mutation($i: String!, $u: IssueUpdateInput!) { issueUpdate(id: $i, input: $u) { success } }",
                 i=issue["id"], u={"stateId": self.states[state], **extra})

    def recover(self):
        """Walk our In Progress issues; returns the resume candidate (issue, sid, k) or None."""
        mine = [(i, self.current_sid(i)) for i in self.issues("In Progress", {"assignee": {"id": {"eq": self.me}}})]
        mine.sort(key=lambda p: (p[1] is None, rank(p[0]), first_line_time(self.entries, p[1]) if p[1] else self.now))
        cand = None
        for issue, sid in mine:
            ident = issue["identifier"]
            latest = latest_sid(self.entries, ident)
            if latest and is_live(self.tdir, latest, self.now):
                continue
            if sid and self.attempts(issue) >= CAP:
                log(f"recover: {ident} reached {CAP} attempts; In Review")
                self.comment_and_move(issue, CAP_COMMENT, "In Review")
            elif sid and os.path.exists(os.path.join(self.tdir, f"{sid}.jsonl")):
                cand = cand or (issue, sid, resume_count(self.entries, sid) + 1)
            elif sid:
                if sid_times(self.entries, sid)[-1] < self.now - LIVE:
                    log(f"recover: {ident} session={sid} has no transcript")
                    self.comment_and_move(issue, INTERRUPTED, "Todo", assigneeId=None)
            elif parse_time(issue["updatedAt"]) < self.now - STALE:
                log(f"recover: {ident} (last updated {issue['updatedAt']})")
                self.comment_and_move(issue, INTERRUPTED, "Todo", assigneeId=None)
        return cand

    def plan(self):
        cand = self.recover()
        if cand:
            issue, sid, k = cand
            log(f"plan: resume {issue['identifier']} session={sid} n={k}")
            return f"resume {issue['identifier']} {sid} {k} {issue['url']}"
        todo = self.issues("Todo")
        if todo:
            log(f"plan: new ({len(todo)} in queue)")
            return "new"
        log("plan: nothing to do")
        return None

    def claim(self):
        # Pick: highest priority first, then oldest.
        queue = sorted(self.issues("Todo"), key=lambda i: (rank(i), i["createdAt"]))
        for issue in queue:
            if self.attempts(issue) >= CAP:
                log(f"pick: {issue['identifier']} reached {CAP} attempts; In Review")
                self.comment_and_move(issue, CAP_COMMENT, "In Review")
                continue
            log(f"pick: {issue['identifier']} ({len(queue)} in queue)")
            if self.dry:
                return None
            # Claim: re-check right before claiming so a concurrent change isn't overwritten.
            current = self.gql("query($i: String!) { issue(id: $i) { state { name } } }", i=issue["id"])["issue"]["state"]["name"]
            if current != "Todo":
                log(f"claim: {issue['identifier']} is now {current}; skipping")
                return None
            self.gql("mutation($i: String!, $s: String!, $a: String!) { issueUpdate(id: $i, input: { stateId: $s, assigneeId: $a }) { success } }",
                     i=issue["id"], s=self.states["In Progress"], a=self.me)
            return f"{issue['identifier']} {issue['url']}"
        log("pick: queue empty")
        return None


def main(argv, gql=linear_gql, now=None, tdir=TRANSCRIPTS, stdin=sys.stdin):
    args = [a for a in argv if a != "--dry-run"]
    dry = len(args) < len(argv)
    mode = args[0] if args[:1] in (["--plan"], ["--claim"], ["--gate"], ["--prune"]) else None
    rest = args[1:] if mode else args
    if (len(rest) > 1 or any(a.startswith("-") for a in rest)
            or mode == "--gate" and rest not in (["resume"], ["new"])
            or mode == "--prune" and (dry or not rest)):
        print(USAGE, file=sys.stderr)
        return 2
    now = now or datetime.now(timezone.utc)
    if mode == "--gate":
        ok, summary = gate(rest[0], stdin)
        print(summary)
        return 0 if ok else 1
    if mode == "--prune":
        prune(rest[0], now)
        return 0
    board = Board(gql, parse_log(rest[0] if rest else RUNS_LOG), tdir, now, dry)
    if mode == "--plan":
        out = board.plan()
    else:
        if mode is None:
            board.recover()
        out = board.claim()
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
