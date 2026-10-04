#!/usr/bin/env python3
"""Router: decides what runs next among the team's issues assigned to role accounts, then calls run.py.

(no mode)           One tick (launchd): hours, per-role locks (all busy -> skip), prune, Recover and plan over idle roles,
                    usage gate, resume or claim, launch.
  --now             Skip the 01:00-06:59 hours check.
  --dry-run         Print the plan and the usage; change nothing, launch nothing.
  --issue ID        With --now: claim this Todo issue instead of the top one; skip if its role is busy.
--brake             Run the usage probe, print the usage, exit 0 if a deep-research round may start (five_hour < 0.8).
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
from config import (PATH, PROJECTS, ROOT, RUNS_LOG, WORK, load_config, role_for, runnable, session,  # noqa: E402
                    stage_order, transcript)
from linear import humans, linear_gql, log, parse_time, role_ids, task_group, team  # noqa: E402

STALE = timedelta(hours=2)
LIVE = timedelta(minutes=30)
CAP = 4
KEEP = timedelta(days=7)
SKEW = timedelta(minutes=5)
MAX_5H = 0.9
BRAKE_5H = 0.8
PROBE = ["claude", "-p", "Reply with OK.", "--model", "haiku", "--output-format", "stream-json", "--verbose",
         "--setting-sources", "user", "--strict-mcp-config"]
CAP_COMMENT = "Tried 4 times without finishing; needs a look."
INTERRUPTED = "The previous run was interrupted. Moving this issue back to the Todo queue."
USAGE = "usage: router.py [--now] [--dry-run] [--issue ID] | --brake"
RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\b")
LINE = re.compile(TS.pattern + r" (start|resume) (\S+) session=(\S+)(?:.* task=(\S+)$)?")
Q_ASSIGNEE = "query($f: IssueFilter) { issues(filter: $f) { nodes { assignee { email } } } }"
DONE = {"completed", "canceled", "duplicate"}
UNREADABLE = "(unreadable)"
RELATIONS = "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"
Q_RELATIONS = "query($i: String!) { issue(id: $i) { " + RELATIONS + " } }"
Q_RECHECK = "query($i: String!) { issue(id: $i) { state { id } labels { nodes { id name parent { id } } } } }"


def local_time(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)


def parse_log(path):
    """(time, kind, issue, sid, task) of every start/resume line, in file order; task is None without a trailing task= field.
    runs.log timestamps are local time."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    entries = []
    for line in lines:
        m = LINE.match(line)
        if m:
            entries.append((local_time(m[1]), m[2], m[3], m[4], m[5]))
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


def logged_task(entries, sid):
    return next((e[4] for e in reversed(entries) if e[3] == sid and e[4]), None)


def sid_times(entries, sid):
    return [e[0] for e in entries if e[3] == sid]


def first_line_time(entries, sid):
    return next((e[0] for e in entries if e[3] == sid and e[1] == "start"), sid_times(entries, sid)[0])


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


def busy_roles(roles, sh):
    """Sorted names of the roles whose session agent-pm-<role> exists; "=" makes tmux match the name exactly, not as a prefix."""
    return sorted(r for r in roles if sh(["tmux", "has-session", "-t", "=" + session(r)], capture_output=True).returncode == 0)


def assignee_email(gql, team_id, ident):
    """Email of the team issue's assignee; None for an unknown or unassigned issue."""
    nodes = gql(Q_ASSIGNEE, f={"team": {"id": {"eq": team_id}}, "id": {"eq": ident}})["issues"]["nodes"]
    return next((n["assignee"]["email"] for n in nodes if n["assignee"]), None)


def rank(issue):
    return issue["priority"] or 5  # 0 = no priority = lowest


def blockers(issue):
    """Identifiers of the issue's unfinished direct blockers in API order, UNREADABLE for a null one; empty = ready."""
    nodes = [n for n in issue["inverseRelations"]["nodes"] if n["type"] == "blocks"]
    return [n["issue"]["identifier"] if n.get("issue") else UNREADABLE for n in nodes
            if not n.get("issue") or n["issue"]["state"]["type"] not in DONE]


def task_for(labels, group, role, role_tasks, label_tasks):
    """(task, None) from the issue's labels in the task label group (none -> role's default), or (None, comment) for an invalid one.
    The task is label_tasks[label id]; only the comment carries label names."""
    found = [n for n in labels if n["parent"] and n["parent"]["id"] == group]
    if not found:
        return role_tasks[0], None
    if len(found) > 1:
        return None, f"Several task labels ({', '.join(n['name'] for n in found)}); keep one, then move the issue back to Todo."
    name = found[0]["name"]
    task = label_tasks.get(found[0]["id"])
    if task is None:
        return None, f'Task label "{name}" is not in orchestrator/config.toml\'s [task_labels]. Fix the label, then move the issue back to Todo.'
    if task not in role_tasks:
        return None, (f'Task label "{name}" is not one of {role}\'s tasks ({", ".join(role_tasks)}). '
                      "Fix the label or the assignee, then move the issue back to Todo.")
    return task, None


def gate(lines, max_5h=MAX_5H):
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
    ok = info.get("status") != "rejected" and five < max_5h and all(v is None or v < 1 for v in week.values())
    return ok, summary


def brake(sh):
    """(ok, summary) of a fresh usage probe against BRAKE_5H: may a deep-research run start another round."""
    probe = sh(PROBE, cwd=WORK, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return gate(probe.stdout.splitlines(), BRAKE_5H)


class Board:
    def __init__(self, gql, entries, tdir, now, dry, cfg, only=None, root=ROOT):
        self.gql, self.entries, self.tdir, self.now, self.dry = gql, entries, tdir, now, dry
        self.hist, self.ready, self.blocked = {}, None, {}
        self.runs = runnable(cfg, root)
        if only is not None:
            if unknown := [r for r in only if r not in self.runs]:
                raise SystemExit(f"no role {unknown[0]!r} in orchestrator/config.toml")
        self.stage = stage_order(cfg)
        self.group, self.label_tasks = cfg["task_label_group"], {i: task for task, i in cfg["task_labels"].items()}
        t = team(gql, cfg)
        task_group(gql, cfg)
        self.team, self.states = t.id, t.states
        self.humans = set(humans(gql, cfg))
        self.emails = cfg.get("human_members") or []
        self.roles = role_ids(gql, {r: run for r, run in self.runs.items() if only is None or r in only})

    def issues(self, state, fields=""):
        flt = {"team": {"id": {"eq": self.team}}, "project": {"null": False},
               "assignee": {"id": {"in": list(self.roles)}}, "state": {"id": {"eq": self.states[state]}}}
        return self.gql("""query($f: IssueFilter) { issues(filter: $f, first: 100) {
                    nodes { id identifier url priority createdAt updatedAt project { id name } assignee { id email } """
                        + fields + " } } }", f=flt)["issues"]["nodes"]

    def todo(self):
        """The ready Todo issues (direct blockers all done), read once per run; logs each blocked one on the first call."""
        if self.ready is None:
            try:
                issues = self.issues("todo", RELATIONS)
            except SystemExit as e:
                log(f"todo: blocker query failed, reading blockers per issue: {e}")
                issues = self.issues("todo")
                for i in issues:
                    try:
                        i.update(self.gql(Q_RELATIONS, i=i["id"])["issue"])
                    except SystemExit:
                        i["inverseRelations"] = {"nodes": [{"type": "blocks", "issue": None}]}
            self.blocked = {i["identifier"]: b for i in issues if (b := blockers(i))}
            for ident, b in self.blocked.items():
                log(f"blocked: {ident} by {', '.join(b)}")
            self.ready = [i for i in issues if i["identifier"] not in self.blocked]
        return self.ready

    def is_blocked(self, ident):
        self.todo()
        return ident in self.blocked

    def role(self, issue):
        return self.roles[issue["assignee"]["id"]]

    def later(self, issue):
        return -self.stage.get(self.role(issue), 0)

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
        """Walk the role accounts' In Progress issues; returns the resume candidate (issue, sid, task) or None."""
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
                role = self.role(issue)
                run = self.runs[role]
                task = logged_task(self.entries, sid) or run.default
                if task not in run.tasks:
                    log(f"recover: {ident} task={task} is not one of {role}'s tasks; In Review")
                    self.comment_and_move(issue, f'The interrupted run\'s task "{task}" is not one of {role}\'s tasks '
                                                 f'({", ".join(run.tasks)}); needs a look.', "in_review")
                else:
                    cand = cand or (issue, sid, task)
            elif sid:
                if sid_times(self.entries, sid)[-1] < self.now - LIVE:
                    log(f"recover: {ident} session={sid} has no transcript")
                    self.comment_and_move(issue, INTERRUPTED, "todo")
            elif parse_time(issue["updatedAt"]) < self.now - STALE:
                log(f"recover: {ident} (last updated {issue['updatedAt']})")
                self.comment_and_move(issue, INTERRUPTED, "todo")
        return cand

    def next_run(self):
        """("resume", issue, sid, task), ("new",) or None, after Recover."""
        cand = self.recover()
        if cand:
            issue, sid, task = cand
            log(f"plan: resume {issue['identifier']} session={sid}")
            return ("resume", issue, sid, task)
        todo = self.todo()
        if todo:
            log(f"plan: new ({len(todo)} in queue)")
            return ("new",)
        log("plan: nothing to do")
        return None

    def take(self, only=None):
        """(claimed Todo issue, its task), or None."""
        # Pick: highest priority first, then later role, then oldest.
        queue = sorted(self.todo(), key=lambda i: (rank(i), self.later(i), i["createdAt"]))
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
            current = self.gql(Q_RECHECK, i=issue["id"])["issue"]
            ident = issue["identifier"]
            if current["state"]["id"] != self.states["todo"]:
                log(f"claim: {ident} is no longer Todo; skipping")
                return None
            role = self.role(issue)
            role_tasks = list(self.runs[role].tasks)
            task, comment = task_for(current["labels"]["nodes"], self.group, role, role_tasks, self.label_tasks)
            if comment:
                log(f"claim: {ident} bad task label; In Review")
                self.comment_and_move(issue, comment, "in_review")
                continue
            log(f"claim: {ident} task={task}")
            self.gql("mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }",
                     i=issue["id"], s=self.states["in_progress"])
            return issue, task
        if queue:
            log("pick: nothing claimable")
        else:
            log(f"pick: {only} is not a Todo issue assigned to a role account" if only else "pick: queue empty")
        return None


def append(path, line):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")


def tick(opts, gql, now, cfg, tdir, runs, sh, hour, root=ROOT):
    """One launchd tick. Returns the exit code."""
    dry, issue_id = opts["dry"], opts["issue"]
    if not opts["now"] and not 1 <= hour <= 6:
        log("skip: outside hours")
        if not dry:
            return 0
    roles = runnable(cfg, root)
    busy = busy_roles(roles, sh)
    if len(busy) == len(roles):
        log(f"skip: all roles busy ({', '.join(busy)})")
        return 0
    if busy:
        log(f"busy: {', '.join(busy)}")
        if issue_id:
            role = role_for(roles, assignee_email(gql, cfg["team"], issue_id))
            if role in busy:
                log(f"skip: {issue_id} belongs to busy role {role}")
                return 0
    if not dry:
        try:
            prune(runs, now)
        except Exception as e:
            log(f"skip: prune failed: {e}")
    # Recover sees only idle roles' issues, so it never touches an issue whose session is running.
    board = Board(gql, parse_log(runs), tdir, now, dry, cfg, only=[r for r in roles if r not in busy], root=root)
    run = board.next_run()
    if issue_id:  # Recover still ran; the requested issue is claimed even if another run could be resumed
        if board.is_blocked(issue_id):
            return 0
        run = ("new",)
    kind = run[0] if run else None
    if not kind and not dry:
        log("skip: nothing to do")
        return 0
    os.makedirs(WORK, exist_ok=True)
    # The usage probe's cwd only, not a run cwd: runs work in work/<ID>/ (run.py).
    probe = sh(PROBE, cwd=WORK, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    ok, usage = gate(probe.stdout.splitlines())
    if dry:
        log(f"plan: {kind or 'nothing'}")
        log(f"usage: {usage} ({kind or 'new'} {'allowed' if ok else 'blocked'})")
        return 0
    if not ok:
        log(f"skip: {kind} blocked by usage: {usage}")
        return 0
    if kind == "resume":
        _, issue, sid, task = run
        append(runs, f"resume {issue['identifier']} session={sid} task={task}")
        mode = ["--mode", "resume"]
    else:
        taken = board.take(issue_id)
        if not taken:
            log("skip: nothing claimed")
            return 0
        issue, task = taken
        sid = str(uuid.uuid4())
        append(runs, f"start {issue['identifier']} session={sid} transcript={transcript(issue['identifier'], sid, tdir)} task={task}")
        mode = ["--mode", "new"]
    ident, project = issue["identifier"], issue["project"]
    rc = sh([sys.executable, RUN, "--issue", ident, "--project", project["id"],
             "--assignee", issue["assignee"]["email"], "--sid", sid, "--task", task] + mode).returncode
    log(f"launch {ident} ({project['name']}) exit={rc}")
    return 0


def main(argv, gql=linear_gql, now=None, tdir=PROJECTS, config=None, runs=RUNS_LOG,
         sh=subprocess.run, hour=None, root=ROOT):
    if "--brake" in argv:
        if argv != ["--brake"]:
            print(USAGE, file=sys.stderr)
            return 2
        os.environ["PATH"] = PATH
        ok, summary = brake(sh)
        print(summary)
        return 0 if ok else 1
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
        return tick(opts, gql, now, cfg(), tdir, runs, sh, datetime.now().hour if hour is None else hour, root)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
