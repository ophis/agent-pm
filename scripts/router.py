#!/usr/bin/env python3
"""Router: decides what runs next among the team's issues assigned to role accounts, then calls launch.py.
docs/specs/2026-09-27-router-launcher-design.md

(no mode)           One tick (launchd): hours, lock, prune, Recover, plan, usage gate, resume or claim, launch.
  --now             Skip the 01:00-06:59 hours check.
  --dry-run         Print the plan and the usage; change nothing, launch nothing.
  --issue ID        With --now: claim this Todo issue instead of the top one.
--pick [--role ROLE] [RUNS_LOG]  Recover, then Pick + Claim (only ROLE's issues if given); print "<ID> <url>" (manual use).
--plan [RUNS_LOG]   Recover, then print "resume <ID> <SID> <k> <url> <project>", "new", or nothing.
--claim [RUNS_LOG]  Pick + Claim: print "<ID> <url> <project>" of the claimed issue, or nothing.
--gate resume|new   Read the usage probe's stream-json on stdin, print the usage, exit 0 if the run may start.
--prune RUNS_LOG    Drop runs.log lines older than 7 days.
Needs Python 3.11+.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import (PATH, PROJECTS, RUNS_LOG, SESSION, WORK, humans, load_config, linear_gql, log,  # noqa: E402
                      parse_time, role_ids, runnable, stage_order, team, transcript)

STALE = timedelta(hours=2)
LIVE = timedelta(minutes=30)
CAP = 4
KEEP = timedelta(days=7)
SKEW = timedelta(minutes=5)
MAX_5H = 0.9
CAP_COMMENT = "Tried 4 times without finishing; needs a look."
INTERRUPTED = "The previous run was interrupted. Moving this issue back to the Todo queue."
USAGE = ("usage: router.py [--now] [--dry-run] [--issue ID] | --pick [--role ROLE] [RUNS_LOG] | [--plan | --claim] [--dry-run] [RUNS_LOG]"
         " | --gate resume|new | --prune RUNS_LOG")
LAUNCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launch.py")
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\b")
LINE = re.compile(TS.pattern + r" (start|resume) (\S+) session=(\S+)")


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


def has_transcript(tdir, issue, sid):
    path = transcript(issue, sid, tdir)
    return path is not None and os.path.exists(path)


def is_live(tdir, issue, sid, now):
    path = transcript(issue, sid, tdir)
    if path is None:
        return False
    cutoff = (now - LIVE).timestamp()
    paths = [path]
    for root, _, files in os.walk(os.path.splitext(path)[0]):
        paths += [os.path.join(root, f) for f in files]
    for p in paths:
        try:
            if os.path.getmtime(p) > cutoff:
                return True
        except OSError:
            pass
    return False


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
    def __init__(self, gql, entries, tdir, now, dry, cfg, only=None):
        self.gql, self.entries, self.tdir, self.now, self.dry = gql, entries, tdir, now, dry
        self.hist = {}
        runs = runnable(cfg)
        if only is not None and only not in runs:
            raise SystemExit(f"no role {only!r} in roles/")
        self.stage = stage_order(cfg)
        t = team(gql, cfg)
        self.team, self.states = t.id, t.states
        self.humans = set(humans(gql, cfg))
        self.emails = cfg.get("human_members") or []
        self.roles = role_ids(gql, {r: run for r, run in runs.items() if only in (None, r)})

    def issues(self, state):
        flt = {"team": {"id": {"eq": self.team}}, "project": {"null": False},
               "assignee": {"id": {"in": list(self.roles)}}, "state": {"id": {"eq": self.states[state]}}}
        return self.gql("""query($f: IssueFilter) { issues(filter: $f, first: 100) {
                    nodes { id identifier url priority createdAt updatedAt project { id name } assignee { id email } } } }""",
                        f=flt)["issues"]["nodes"]

    def later(self, issue):
        return -self.stage.get(self.roles[issue["assignee"]["id"]], 0)

    def last_move(self, issue, state, by_user=False):
        """Latest time the issue was moved to state (by_user: by a `human_members` user)."""
        if issue["id"] not in self.hist:
            # orderBy createdAt returns newest first, so the latest moves are on this page.
            self.hist[issue["id"]] = self.gql("""query($i: String!) { issue(id: $i) { history(first: 250, orderBy: createdAt) {
                    nodes { createdAt actorId toStateId } } } }""", i=issue["id"])["issue"]["history"]["nodes"]
        times = [parse_time(n["createdAt"]) for n in self.hist[issue["id"]] if n["toStateId"] == self.states[state]
                 and (not by_user or n["actorId"] in self.humans)]
        return max(times, default=None)

    def attempts(self, issue):
        n = attempt_count(self.entries, issue["identifier"])
        return n if n < CAP else attempt_count(self.entries, issue["identifier"], self.last_move(issue, "todo", by_user=True))

    def current_sid(self, issue):
        """The issue's latest SID, unless it began before the issue's latest move to In Progress (minus SKEW)."""
        sid = latest_sid(self.entries, issue["identifier"])
        if not sid:
            return None
        moved = self.last_move(issue, "in_progress")
        return sid if moved is None or first_line_time(self.entries, sid) >= moved - SKEW else None

    def comment_and_move(self, issue, body, state):
        if self.dry:
            return
        if state == "in_review":
            for email in self.emails:
                self.gql("mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }",
                         i=issue["id"], e=email)
        self.gql("mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }",
                 i=issue["id"], b=body)
        self.gql("mutation($i: String!, $u: IssueUpdateInput!) { issueUpdate(id: $i, input: $u) { success } }",
                 i=issue["id"], u={"stateId": self.states[state]})

    def recover(self):
        """Walk the role accounts' In Progress issues; returns the resume candidate (issue, sid, k) or None."""
        mine = [(i, self.current_sid(i)) for i in self.issues("in_progress")]
        mine.sort(key=lambda p: (p[1] is None, rank(p[0]), self.later(p[0]),
                                 first_line_time(self.entries, p[1]) if p[1] else self.now))
        cand = None
        for issue, sid in mine:
            ident = issue["identifier"]
            latest = latest_sid(self.entries, ident)
            if latest and is_live(self.tdir, ident, latest, self.now):
                continue
            if sid and self.attempts(issue) >= CAP:
                log(f"recover: {ident} reached {CAP} attempts; In Review")
                self.comment_and_move(issue, CAP_COMMENT, "in_review")
            elif sid and has_transcript(self.tdir, ident, sid):
                cand = cand or (issue, sid, resume_count(self.entries, sid) + 1)
            elif sid:
                if sid_times(self.entries, sid)[-1] < self.now - LIVE:
                    log(f"recover: {ident} session={sid} has no transcript")
                    self.comment_and_move(issue, INTERRUPTED, "todo")
            elif parse_time(issue["updatedAt"]) < self.now - STALE:
                log(f"recover: {ident} (last updated {issue['updatedAt']})")
                self.comment_and_move(issue, INTERRUPTED, "todo")
        return cand

    def next_run(self):
        """("resume", issue, sid, k), ("new",) or None, after Recover."""
        cand = self.recover()
        if cand:
            issue, sid, k = cand
            log(f"plan: resume {issue['identifier']} session={sid} n={k}")
            return ("resume", issue, sid, k)
        todo = self.issues("todo")
        if todo:
            log(f"plan: new ({len(todo)} in queue)")
            return ("new",)
        log("plan: nothing to do")
        return None

    def plan(self):
        run = self.next_run()
        if run and run[0] == "resume":
            _, issue, sid, k = run
            return f"resume {issue['identifier']} {sid} {k} {issue['url']} {issue['project']['name']}"
        return run and "new"

    def take(self, only=None):
        """The claimed Todo issue, or None."""
        # Pick: highest priority first, then later role, then oldest.
        queue = sorted(self.issues("todo"), key=lambda i: (rank(i), self.later(i), i["createdAt"]))
        if only:
            queue = [i for i in queue if i["identifier"] == only]
        for issue in queue:
            if self.attempts(issue) >= CAP:
                log(f"pick: {issue['identifier']} reached {CAP} attempts; In Review")
                self.comment_and_move(issue, CAP_COMMENT, "in_review")
                continue
            log(f"pick: {issue['identifier']} ({len(queue)} in queue)")
            if self.dry:
                return None
            # Claim: re-check right before claiming so a concurrent change isn't overwritten.
            current = self.gql("query($i: String!) { issue(id: $i) { state { id } } }", i=issue["id"])["issue"]["state"]["id"]
            if current != self.states["todo"]:
                log(f"claim: {issue['identifier']} is no longer Todo; skipping")
                return None
            self.gql("mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }",
                     i=issue["id"], s=self.states["in_progress"])
            return issue
        log(f"pick: {only} is not a Todo issue assigned to a role account" if only else "pick: queue empty")
        return None

    def claim(self, only=None):
        issue = self.take(only)
        return issue and f"{issue['identifier']} {issue['url']} {issue['project']['name']}"


def append(path, line):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")


def tick(opts, gql, now, cfg, tdir, runs, sh, hour):
    """One launchd tick. Returns the exit code."""
    dry, issue_id = opts["dry"], opts["issue"]
    if not opts["now"] and not 1 <= hour <= 6:
        log("skip: outside hours")
        if not dry:
            return 0
    # Checked before Recover: Recover assumes no run is active.
    if sh(["tmux", "has-session", "-t", SESSION], capture_output=True).returncode == 0:
        log("skip: previous run still active")
        if not dry:
            return 0
    if not dry:
        try:
            prune(runs, now)
        except Exception as e:
            log(f"skip: prune failed: {e}")
    board = Board(gql, parse_log(runs), tdir, now, dry, cfg)
    run = board.next_run()
    if issue_id:  # Recover still ran; the requested issue is claimed even if another run could be resumed
        run = ("new",)
    kind = run[0] if run else None
    if not kind and not dry:
        log("skip: nothing to do")
        return 0
    os.makedirs(WORK, exist_ok=True)
    # The usage probe's cwd only, not a run cwd: runs work in work/<ID>/ (launch.py).
    probe = sh(["claude", "-p", "Reply with OK.", "--model", "haiku", "--output-format", "stream-json", "--verbose"],
               cwd=WORK, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    ok, usage = gate(kind or "new", probe.stdout.splitlines())
    if dry:
        log(f"plan: {kind or 'nothing'}")
        log(f"usage: {usage} ({kind or 'new'} {'allowed' if ok else 'blocked'})")
        return 0
    if not ok:
        log(f"skip: {kind} blocked by usage: {usage}")
        return 0
    if kind == "resume":
        _, issue, sid, k = run
        append(runs, f"resume {issue['identifier']} session={sid} n={k}")
        mode = ["--mode", "resume", "--k", str(k)]
    else:
        issue = board.take(issue_id)
        if not issue:
            log("skip: nothing claimed")
            return 0
        sid = str(uuid.uuid4())
        append(runs, f"start {issue['identifier']} session={sid} transcript={transcript(issue['identifier'], sid, tdir)}")
        mode = ["--mode", "new"]
    ident, project = issue["identifier"], issue["project"]
    rc = sh([sys.executable, LAUNCH, "--issue", ident, "--url", issue["url"], "--project", project["id"],
             "--assignee", issue["assignee"]["email"], "--sid", sid] + mode).returncode
    log(f"launch {ident} ({project['name']}) exit={rc}")
    return 0


def main(argv, gql=linear_gql, now=None, tdir=PROJECTS, stdin=sys.stdin, config=None, runs=RUNS_LOG,
         sh=subprocess.run, hour=None):
    args = [a for a in argv if a != "--dry-run"]
    dry = len(args) < len(argv)
    now = now or datetime.now(timezone.utc)
    cfg = lambda: load_config(config) if config else load_config()  # noqa: E731
    if not args or args[0] in ("--now", "--issue"):
        opts = {"dry": dry, "now": "--now" in args, "issue": None}
        rest = [a for a in args if a != "--now"]
        if rest[:1] == ["--issue"] and len(rest) == 2 and opts["now"]:
            opts["issue"] = rest[1]
        elif rest:
            print(USAGE, file=sys.stderr)
            return 2
        os.environ["PATH"] = PATH
        return tick(opts, gql, now, cfg(), tdir, runs, sh, datetime.now().hour if hour is None else hour)
    if args[0] == "--pick":
        rest, only = args[1:], None
        if rest[:1] == ["--role"] and len(rest) >= 2:
            only, rest = rest[1], rest[2:]
        if len(rest) > 1 or any(a.startswith("-") for a in rest):
            print(USAGE, file=sys.stderr)
            return 2
        board = Board(gql, parse_log(rest[0] if rest else runs), tdir, now, dry, cfg(), only=only)
        board.recover()
        out = board.claim()
        if out:
            print(" ".join(out.split()[:2]))
        return 0
    mode = args[0] if args[:1] in (["--plan"], ["--claim"], ["--gate"], ["--prune"]) else None
    rest = args[1:] if mode else args
    if (not mode or len(rest) > 1 or any(a.startswith("-") for a in rest)
            or mode == "--gate" and rest not in (["resume"], ["new"])
            or mode == "--prune" and (dry or not rest)):
        print(USAGE, file=sys.stderr)
        return 2
    if mode == "--gate":
        ok, summary = gate(rest[0], stdin)
        print(summary)
        return 0 if ok else 1
    if mode == "--prune":
        prune(rest[0], now)
        return 0
    board = Board(gql, parse_log(rest[0] if rest else runs), tdir, now, dry, cfg())
    out = board.plan() if mode == "--plan" else board.claim()
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
