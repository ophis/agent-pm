#!/usr/bin/env python3
"""Router: the orchestrator's one entry point. Decides which agent run starts or resumes among the team's issues
assigned to role accounts, then starts it through the outer; each step: CLAUDE.md › Architecture.

(no mode)           One tick (launchd): the hours check, then at most one agent run.
  --now             Skip the 01:00-06:59 hours check.
--issue ID          Start or resume only this issue, without the hours, max_runs and usage gates. Exits 2 bad
                    arguments, 1 a config error, Linear unavailable (linear-error, once a day), a live agent run of the
                    issue or nothing started, else the outer's code.
--dry-run           With either: print the plan (a tick's: and one usage probe); change nothing, launch nothing; exit 0.
--tui [--split right|below] [--split-from SESSION] [--events FILE] [--manager NAME]
                    With either: the agent run is attended (the tui runner); the TUI pane's place and the events file
                    are checked first (exit 2 when one fails). A tmux grid ignores --split and --split-from. Without
                    --events the file is the manager directory's (core/src/manager.py; NAME, else this tmux session);
                    outside tmux with neither, there is none.
--brake             Run the usage probe, print the usage, exit 0 if a deep-research round may start.

Events go to orchestrator.jsonl (linear.log, src router); an idle tick writes none.
"""
import argparse
import contextlib
import fcntl
import functools
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
from config import (LOCAL, PATH, PROJECTS, ROOT, RUNS_LOG, RUNS_DIR, TASKS, UUID_RE, load_config, role_for,  # noqa: E402
                    run_dir, runnable, session, sh_run, stage_order, transcript)
import attended  # noqa: E402
import inputs  # noqa: E402
import issues  # noqa: E402
import linear  # noqa: E402
import target  # noqa: E402
import writeback  # noqa: E402
from linear import (CONFIG_ERRORS, ISSUE_ID, config_error, humans, linear_gql, log, one_line, parse_time,  # noqa: E402
                    role_ids, task_group, team)
import drive  # noqa: E402
import manager  # noqa: E402
import tui_claude  # noqa: E402

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
INTERRUPTED = "The previous agent run was interrupted. Moving this issue back to the Todo queue."
BUSY = "another router is running"
USAGE = ("usage: router.py [--now] [--dry-run] [--issue ID] "
         "[--tui [--split right|below] [--split-from SESSION] [--events FILE] [--manager NAME]] | --brake")
RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
SHARED = ("issue", "project", "assignee", "sid", "task", "mode")
DONE = {"completed", "canceled", "duplicate"}
UNREADABLE = "(unreadable)"
RELATIONS = "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"
Q_RELATIONS = "query($i: String!) { issue(id: $i) { " + RELATIONS + " } }"
Q_RECHECK = "query($i: String!) { issue(id: $i) { state { id } labels { nodes { id name parent { id } } } } }"
Q_HISTORY = "query($i: String!) { issue(id: $i) { " + linear.HISTORY + " } }"


def parse_line(line):
    """(utc time, kind, issue, sid, role) of a runs.jsonl start/resume line, else None."""
    try:
        d = json.loads(line)
        if d["kind"] in ("start", "resume") and all(isinstance(d[k], str) for k in ("issue", "sid", "role")):
            return datetime.fromisoformat(d["ts"]).astimezone(timezone.utc), d["kind"], d["issue"], d["sid"], d["role"]
    except (ValueError, TypeError, KeyError, OverflowError):
        pass


def parse_log(path):
    """parse_line of every line of the runs.jsonl at path that has one, in file order."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    return [e for line in lines if (e := parse_line(line))]


def prune(path, now):
    """Drops the lines older than KEEP and those parse_log skips; rewrites the file only then."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    keep = [line for line in lines if (e := parse_line(line)) and e[0] >= now - KEEP]
    if len(keep) < len(lines):
        drive.save(path, "".join(keep))


def latest_sid(entries, issue):
    return next((e[3] for e in reversed(entries) if e[2] == issue), None)


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


def live_sessions(roles, sh):
    """{role: sorted IDs of the issues with a session(role, ID)} for every role in roles, from one tmux list-sessions;
    other names are ignored, and no tmux server means no sessions."""
    out = sh(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True)
    names = out.stdout.splitlines() if out.returncode == 0 else []
    live = {}
    for r in roles:
        prefix = session(r, "")
        ids = [n.removeprefix(prefix) for n in names if n.startswith(prefix)]
        live[r] = sorted(i for i in ids if re.fullmatch(linear.ISSUE_ID, i))
    return live


def full_roles(roles, live):
    """The roles with max_runs or more live sessions."""
    return {r for r in roles if len(live[r]) >= roles[r].max_runs}


def rank(issue):
    return issue["priority"] or 5  # 0 = no priority = lowest


def blockers(issue):
    """Identifiers of the issue's unfinished direct blockers in API order, UNREADABLE for a null one; empty = ready."""
    nodes = [n for n in issue["inverseRelations"]["nodes"] if n["type"] == "blocks"]
    return [n["issue"]["identifier"] if n.get("issue") else UNREADABLE for n in nodes
            if not n.get("issue") or n["issue"]["state"]["type"] not in DONE]


def task_for(labels, group, role, role_tasks, label_tasks):
    """(task, None) from the issue's labels in the task label group (none: (None, None), the agent run picks), or
    (None, comment) for an invalid one. The task is label_tasks[label id]; only the comment carries label names."""
    found = [n for n in labels if n["parent"] and n["parent"]["id"] == group]
    if not found:
        return None, None
    if len(found) > 1:
        return None, f"Several task labels ({', '.join(n['name'] for n in found)}); keep one, then move the issue back to Todo."
    name = found[0]["name"]
    task = label_tasks.get(found[0]["id"])
    if task is None:
        return None, f'Task label "{name}" is not in {LOCAL}\'s [task_labels]. Fix the label, then move the issue back to Todo.'
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


def probe(sh, max_5h=MAX_5H):
    """(ok, summary) of a fresh usage probe against max_5h. Its cwd is <work_dir>/work itself, not an agent run's <work_dir>/work/<ID>/."""
    os.makedirs(RUNS_DIR, exist_ok=True)
    out = sh(PROBE, cwd=RUNS_DIR, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return gate(out.stdout.splitlines(), max_5h)


def q_issues(fields=""):
    """Board.issues's query text, fields added to each issue's."""
    return ("query($f: IssueFilter) { issues(filter: $f, first: 100) { nodes { id identifier url priority createdAt "
            "updatedAt project { id name } assignee { id email } " + fields + " } } }")


class Board:
    def __init__(self, gql, entries, tdir, now, dry, cfg, root=ROOT):
        self.gql, self.entries, self.tdir, self.now, self.dry = gql, entries, tdir, now, dry
        self.say = functools.partial(log, "router", dry=dry)
        self.hist, self.ready, self.blocked, self.live = {}, None, {}, set()
        self.runs = runnable(cfg, root)
        self.stage = stage_order(cfg)
        self.group, self.label_tasks = cfg["task_label_group"], {i: task for task, i in cfg["task_labels"].items()}
        t = team(gql, cfg)
        task_group(gql, cfg)
        self.team, self.states = t.id, t.states
        self.humans = set(humans(gql, cfg))
        self.emails = cfg.get("human_members") or []
        self.roles = role_ids(gql, self.runs)

    def issues(self, state, fields=""):
        flt = {"team": {"id": {"eq": self.team}}, "project": {"null": False},
               "assignee": {"id": {"in": list(self.roles)}}, "state": {"id": {"eq": self.states[state]}}}
        return self.gql(q_issues(fields), f=flt)["issues"]["nodes"]

    def todo(self):
        """The ready Todo issues (direct blockers all done), read once per run; logs each blocked one (once a day) on
        the first call."""
        if self.ready is None:
            try:
                issues = self.issues("todo", RELATIONS)
            except SystemExit as e:
                self.say("todo-error", error=one_line(e))
                issues = self.issues("todo")
                for i in issues:
                    try:
                        i.update(self.gql(Q_RELATIONS, i=i["id"])["issue"])
                    except SystemExit:
                        i["inverseRelations"] = {"nodes": [{"type": "blocks", "issue": None}]}
            self.blocked = {i["identifier"]: b for i in issues if (b := blockers(i))}
            for ident, b in self.blocked.items():
                self.say("blocked", ident, once=True, by=b)
            self.ready = [i for i in issues if i["identifier"] not in self.blocked]
        return self.ready

    def is_blocked(self, ident):
        self.todo()
        return ident in self.blocked

    def role(self, issue):
        return self.roles[issue["assignee"]["id"]]

    def available(self, issue, full):
        """Without a live session and of a role not in full."""
        return issue["identifier"] not in self.live and self.role(issue) not in full

    def later(self, issue):
        return -self.stage.get(self.role(issue), 0)

    def last_move(self, issue, state, by_user=False):
        """Latest time the issue was moved to state (by_user: by a `human_members` user)."""
        if issue["id"] not in self.hist:
            self.hist[issue["id"]] = self.gql(Q_HISTORY, i=issue["id"])["issue"]["history"]["nodes"]
        return linear.last_move(self.hist[issue["id"]], {self.states[state]}, self.humans if by_user else None)

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

    def comment_and_move(self, issue, body, state, frm, step):
        """linear.comment_and_move from frm to state, the humans subscribed for In Review; a skipped move is logged."""
        if self.dry:
            return
        left = linear.comment_and_move(self.gql, issue["id"], body, self.states[state], self.states[frm],
                                       self.emails if state == "in_review" else ())
        if left is not None:
            self.say("move-skip", issue["identifier"], step=step, state=left)

    def recover(self, live_ids=()):
        """Settle the role accounts' In Progress issues, leaving those in live_ids (IDs with a session; take skips them too)
        untouched; returns the resume candidates [(issue, sid)] in resume order."""
        self.live = set(live_ids)
        mine = [(i, self.current_sid(i)) for i in self.issues("in_progress") if i["identifier"] not in self.live]
        mine.sort(key=lambda p: (p[1] is None, rank(p[0]), self.later(p[0]),
                                 first_line_time(self.entries, p[1]) if p[1] else self.now))
        return [(issue, sid) for issue, sid in mine if self.settle(issue, sid)]

    def settle(self, issue, sid, wait=True):
        """Recover's decision for one In Progress issue without a live session, sid its current_sid: True to resume sid,
        else False, having acted (the attempts cap or a logged role not the assignee's → In Review; no sid or
        no transcript → Todo). wait (the tick's) leaves the issue alone while its latest transcript changed within
        LIVE, a sid without a transcript until LIVE after its last line, and no sid until STALE after the last update;
        --issue trusts tmux ls."""
        ident = issue["identifier"]
        if wait and (latest := latest_sid(self.entries, ident)) and is_live(self.tdir, ident, latest, self.now):
            return False
        if sid and self.attempts(issue) >= CAP:
            self.say("recover", ident, to="in_review", reason=f"reached {CAP} attempts")
            self.comment_and_move(issue, CAP_COMMENT, "in_review", "in_progress", "recover")
        elif sid and has_transcript(self.tdir, ident, sid):
            role, logged = self.role(issue), next(e[4] for e in reversed(self.entries) if e[3] == sid)
            if logged == role:
                return True
            why = f"role ({logged}) is not the assignee's ({role})"
            self.say("recover", ident, to="in_review", reason=why)
            self.comment_and_move(issue, f"The interrupted agent run's {why}; needs a look.",
                                  "in_review", "in_progress", "recover")
        elif sid:
            if not wait or sid_times(self.entries, sid)[-1] < self.now - LIVE:
                self.say("recover", ident, to="todo", reason="no transcript", sid=sid)
                self.comment_and_move(issue, INTERRUPTED, "todo", "in_progress", "recover")
        elif not wait or parse_time(issue["updatedAt"]) < self.now - STALE:
            self.say("recover", ident, to="todo", reason="no session", updated=issue["updatedAt"])
            self.comment_and_move(issue, INTERRUPTED, "todo", "in_progress", "recover")
        return False

    def queue(self, full=()):
        """The available ready Todo issues, highest priority first, then later role, then oldest."""
        return sorted((i for i in self.todo() if self.available(i, full)), key=lambda i: (rank(i), self.later(i), i["createdAt"]))

    def next_run(self, cands, full=()):
        """("resume", issue, sid) for the first available of Recover's cands; else ("new", queue length) while the queue
        holds an issue; else None."""
        cand = next((c for c in cands if self.available(c[0], full)), None)
        if cand:
            return ("resume", *cand)
        if queue := self.queue(full):
            return ("new", len(queue))
        return None

    def plan(self, run, live=None):
        """Logs next_run's run, with live: the live session counts per role."""
        if run[0] == "resume":
            self.say("plan", run[1]["identifier"], mode="resume", sid=run[2], live=live)
        else:
            self.say("plan", mode="new", queue=run[1], live=live)

    def take(self, only=None, full=()):
        """(claimed Todo issue, its label's task or None) from the queue, or None. Dry: (pick, None), unclaimed."""
        queue = self.queue(full)
        if only:
            queue = [i for i in queue if i["identifier"] == only]
        for issue in queue:
            ident = issue["identifier"]
            if self.attempts(issue) >= CAP:
                self.say("claim-skip", ident, to="in_review", reason=f"reached {CAP} attempts")
                self.comment_and_move(issue, CAP_COMMENT, "in_review", "todo", "pick")
                continue
            self.say("pick", ident, queue=len(queue))
            if self.dry:
                return issue, None
            # Claim: re-check right before claiming so a concurrent change isn't overwritten.
            current = self.gql(Q_RECHECK, i=issue["id"])["issue"]
            if current["state"]["id"] != self.states["todo"]:
                self.say("claim-skip", ident, reason="no longer Todo")
                continue
            role = self.role(issue)
            task, comment = task_for(current["labels"]["nodes"], self.group, role, self.runs[role].tasks,
                                     self.label_tasks)
            if comment:
                self.say("claim-skip", ident, to="in_review", reason="bad task label")
                self.comment_and_move(issue, comment, "in_review", "todo", "claim")
                continue
            self.say("claim", ident, role=role, task=task)
            linear.call(self.gql, linear.M_STATE, "issueUpdate", i=issue["id"], s=self.states["in_progress"])
            return issue, task
        if queue:
            self.say("pick-none", reason="nothing claimable")
        else:
            self.say("pick-none", only, reason="not a Todo issue assigned to a role account" if only else "queue empty")
        return None


def fail(issue, kind, reason, rc):
    """An agent run that does not start: one `<kind>` event; returns the exit code."""
    log("router", kind, issue, reason=reason)
    return rc


def _humans(cfg):
    """The human_members emails, lowercased, in config order."""
    return tuple(e.lower() for e in cfg.get("human_members") or [])


def context(a, cfg, name, role, gql, issue_id, repo):
    """The agent run's write-back Context, acting as the role account."""
    return writeback.Context(
        ident=a.issue, issue_id=issue_id, role=name, sid=a.sid, resume=a.mode == "resume", project=a.project,
        workdir=run_dir(a.issue), gql=functools.partial(gql, service=role.key), humans=_humans(cfg),
        states=cfg["states"], repos=cfg["project_repos"], team=cfg["team"], target=repo)


class Setup(Exception):
    """An agent run that cannot start: message and exit code (1 config, 2 role or task)."""

    def __init__(self, msg, rc):
        super().__init__(msg)
        self.msg, self.rc = msg, rc


def load(root):
    """(cfg, roles) of root's orchestrator/config.toml, else Setup."""
    try:
        cfg = load_config(os.path.join(root, "orchestrator", "config.toml"))
        return cfg, runnable(cfg, root)
    except SystemExit as e:
        raise Setup(one_line(e.code), 1)


def setup(a, root):
    """(cfg, roles, name, role) for the agent run's assignee and task (None: the run picks), else Setup."""
    cfg, roles = load(root)
    name = role_for(roles, a.assignee)
    if name is None:
        raise Setup(f"{a.assignee!r} is not a role account", 2)
    role = roles[name]
    if a.task is not None and a.task not in role.tasks:
        raise Setup(f"task {a.task!r} is not one of {name}'s tasks ({', '.join(role.tasks)})", 2)
    return cfg, roles, name, role


def pipeline(a, driver, tui, layout, rd, sh):
    """drive.detach's roster for a tui run of a.issue, rd its run dir: (d, the pipeline entry's fields), d its roster
    directory (manager.home of a.manager and a.events); None without one. A ManagerError prints its line
    (manager.unwritten)."""
    if a.events is None:   # home would say None, after an own_session tmux call
        return None
    try:
        d = manager.home(a.manager, a.events, proc=sh)
    except manager.ManagerError as e:
        manager.unwritten(driver, e)
        return None
    if d is None:
        return None
    return d, {"kind": "pipeline", "sid": a.sid, "cwd": rd, "note": a.issue, "tui": tui, "opener": layout.opener,
               "split": layout.split, "split_from": layout.split_from,
               "resume": [sys.executable, os.path.abspath(__file__), "--issue", a.issue, "--tui", "--events",
                          os.path.join(d, "events"), "--manager", os.path.basename(d)]}


def outer(a, *, sh, gql, run, projects, keychain, root):
    """Checks, bounces or prepares the agent run (a: SHARED and the tui runner's options), then starts the inner in tmux.
    Exits 0 started or bounced, 1 config, input or tmux failure, 2 a bad id, not a role account, config error or a failed
    tui check, 3 transient; a config error, transient failure, bounce, input or tmux failure is logged (src router).
    The tui runner's attach commands go to stderr."""
    os.environ["PATH"] = PATH
    if not re.fullmatch(ISSUE_ID, a.issue) or not UUID_RE.fullmatch(a.sid):
        print(f"router.py: bad issue or session id: {a.issue} {a.sid}", file=sys.stderr)
        return 2
    layout = events = None
    if a.runner == "tui":
        try:
            layout = attended.layout(a.split, a.split_from)
            events = tui_claude.events_file(a.events) if a.events is not None else None
        except (attended.Bad, tui_claude.TuiError) as e:
            print(f"router.py: {e}", file=sys.stderr)
            return 2
    try:
        cfg, roles, name, role = setup(a, root)
    except Setup as e:
        return fail(a.issue, "config-error", e.msg, e.rc)
    if layout:
        try:
            tui = drive.tui_session(name, a.sid, prefix=attended.prefix(name, a.issue))
        except ValueError as e:
            return fail(a.issue, "config-error", one_line(e), 2)
    if a.mode == "resume":
        path = transcript(a.issue, a.sid, projects)
        if path is None or not os.path.exists(path):
            return fail(a.issue, "transient", f"no transcript to resume at {path}", 3)
    if not keychain(role.key):
        return fail(a.issue, "config-error", f"no Keychain item for role key {role.key}", 2)
    try:
        issue = issues.read_issue(gql, a.issue)
    except (Exception, SystemExit) as e:  # SystemExit: linear_gql's API error
        return fail(a.issue, "transient", f"Linear: {one_line(e)}", 3)
    kind, repos, repo = TASKS[name].kind, cfg["project_repos"], None
    if kind == "build":
        try:
            repo = target.check(issue, repos, run=run, work=config.RUNS_DIR)
        except Exception as e:  # a gh/git timeout or OS error
            return fail(a.issue, "transient", f"repo check: {one_line(e)}", 3)
        if isinstance(repo, target.Transient) or isinstance(repo, target.Invalid) and a.mode == "resume":
            return fail(a.issue, "transient", repo.reason, 3)  # never bounce a mid-build agent run
        if isinstance(repo, target.Invalid):
            try:
                writeback.bounce(context(a, cfg, name, role, gql, issue.id, None), issue, repo.reason)
            except (Exception, SystemExit) as e:
                return fail(a.issue, "transient", f"bounce: {one_line(e)}", 3)
            log("router", "bounce", a.issue, reason=repo.reason)
            return 0
    else:
        repo = target.research_repo(issue, repos)
    if repo is not None:
        repo = target.with_clone(repo, a.issue, cfg["local_clones"], work=config.RUNS_DIR, run=run)
    rd = run_dir(a.issue)
    docs = config.docs(roles, root)
    try:
        sources = inputs.gather(issue, name, docs, run=run)
    except Exception as e:  # gh's RuntimeError; a decode, timeout or OS error too
        return fail(a.issue, "transient", f"docs: {one_line(e)}", 3)
    try:
        os.makedirs(rd, exist_ok=True)
        text = inputs.render(issue, name, sources, humans=_humans(cfg), target=repo, docs=docs).replace("\0", "")
        text = text.encode("utf-8", "replace").decode()   # a lone surrogate would fail execve in the pane
    except Exception as e:
        log("router", "launch-error", a.issue, error=f"input: {one_line(e)}")
        return 1
    attended_argv, iterm, roster = [], "", None
    if layout:
        iterm = os.environ.get("ITERM_SESSION_ID", "")  # tmux's own may be stale
        given = (("split", layout.split), ("split-from", layout.split_from), ("opener", layout.opener), ("events", events))
        attended_argv = ["--runner=tui", *(f"--{k}={v}" for k, v in given if v is not None)]
        print(f"router.py: driver: {tui_claude.attach_command(session(name, a.issue))}", file=sys.stderr)
        print(f"router.py: tui: {tui_claude.attach_command(tui)}", file=sys.stderr)
        roster = pipeline(a, session(name, a.issue), tui, layout, rd, sh)
    try:  # the input goes through the handover file: tmux rejects a command over about 16 KB
        drive.detach(session(name, a.issue), [
            sys.executable, RUN, "--uuid", issue.id,
            *(["--target", f"{repo.owner}/{repo.name}"] if kind == "build" else []),
            *(f"--{k}={v}" for k in SHARED if (v := getattr(a, k)) is not None), *attended_argv, f"--input={text}"],
            cwd=rd, env=os.environ, iterm=iterm, proc=sh, roster=roster)
    except drive.RunnerError as e:
        log("router", "launch-error", a.issue, error=one_line(e))
        return 1
    return 0


def begin(mode, issue, sid, role, task, opts, runs):
    """Appends the agent run's start (mode new) or resume line to runs.jsonl; returns the outer's arguments."""
    ident = issue["identifier"]
    os.makedirs(os.path.dirname(runs), exist_ok=True)
    drive.note(runs, "resume" if mode == "resume" else "start", issue=ident, sid=sid, role=role)
    return argparse.Namespace(issue=ident, project=issue["project"]["id"], assignee=issue["assignee"]["email"], sid=sid,
                              task=task, mode=mode, runner="tui" if opts["tui"] else "headless", split=opts["split"],
                              split_from=opts["split_from"], events=opts["events"], manager=opts["manager"])


@contextlib.contextmanager
def lock(runs, dry):
    """One router at a time: yields True holding <runs' dir>/router.lock (flock; the OS frees it if the process dies),
    or False while another router holds it. A dry run takes none: True."""
    if dry:
        yield True
        return
    os.makedirs(os.path.dirname(runs), exist_ok=True)
    with open(os.path.join(os.path.dirname(runs), "router.lock"), "a") as f:  # not inherited by children (PEP 446)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free = True
        except BlockingIOError:
            free = False
        yield free


def tick(opts, gql, now, cfg, tdir, runs, sh, hour, root, start):
    """One launchd tick; start(outer's arguments) starts the agent run. Returns the exit code. An idle tick (outside
    hours, every role full, nothing to do) logs nothing, a dry run still prints the first two; the plan is logged
    once the usage gate passes (a dry run: before the usage)."""
    dry = opts["dry"]
    say = functools.partial(log, "router", dry=dry)
    if not opts["now"] and not 1 <= hour <= 6:
        if not dry:
            return 0
        say("skip", reason="outside hours")
    with lock(runs, dry) as free:
        if not free:
            say("skip", reason=BUSY)
            return 0
        roles = runnable(cfg, root)
        live = live_sessions(roles, sh)
        full = full_roles(roles, live)
        counts = {r: f"{len(ids)}/{roles[r].max_runs}" for r, ids in sorted(live.items()) if ids} or None
        if len(full) == len(roles):
            if dry:
                say("skip", reason="all roles full", live=counts)
            return 0
        live_ids = {i for ids in live.values() for i in ids}
        if not dry:
            try:
                prune(runs, now)
            except Exception as e:
                say("skip", reason="prune failed", error=one_line(e))
        board = Board(gql, parse_log(runs), tdir, now, dry, cfg, root=root)
        run = board.next_run(board.recover(live_ids), full)
        kind = run[0] if run else None
        if not kind and not dry:
            return 0
        if dry:
            if kind:
                board.plan(run, counts)
            planned = kind == "resume" or kind == "new" and board.take(full=full) is not None
            ok, usage = probe(sh)
            say("usage", usage=usage, planned=planned, allowed=ok)
            return 0
        ok, usage = probe(sh)
        if not ok:
            say("usage-skip", once=True, mode=kind, reason="blocked by usage", usage=usage)
            return 0
        board.plan(run, counts)
        if kind == "resume":
            (_, issue, sid), task = run, None
        else:
            taken = board.take(full=full)
            if not taken:
                say("skip", reason="nothing claimed")
                return 0
            (issue, task), sid = taken, str(uuid.uuid4())
        a = begin(kind, issue, sid, board.role(issue), task, opts, runs)
        try:
            rc = start(a)
        except (Exception, SystemExit) as e:  # SystemExit: linear_gql's API error; one issue's failure never breaks the tick
            say("launch-error", a.issue, error=one_line(e))
            return 0
        say("launch", a.issue, exit=rc)
        return 0


def run_issue(opts, gql, now, cfg, tdir, runs, sh, root, start):
    """router.py --issue ID: nothing while another router runs; refuse a live agent run of the issue (its attach
    command); In Progress → settle without waiting, then resume; Todo → take, then start (start: as tick's). Returns
    the exit code."""
    ident, dry = opts["issue"], opts["dry"]
    stop = 0 if dry else 1  # nothing started
    with lock(runs, dry) as free:
        if not free:
            log("router", "skip", ident, reason=BUSY)
            print(f"router.py: {BUSY}; try again", file=sys.stderr)
            return stop
        live = live_sessions(runnable(cfg, root), sh)
        if role := next((r for r, ids in live.items() if ident in ids), None):
            print(f"router.py: {ident} has a live agent run: {tui_claude.attach_command(session(role, ident))}", file=sys.stderr)
            return stop
        board = Board(gql, parse_log(runs), tdir, now, dry, cfg, root=root)
        if issue := next((i for i in board.issues("in_progress") if i["identifier"] == ident), None):
            sid = board.current_sid(issue)
            if not board.settle(issue, sid, wait=False):
                return stop
            board.plan(("resume", issue, sid))
            mode, task = "resume", None
        elif board.is_blocked(ident) or not (taken := board.take(ident)):
            return stop
        else:
            (issue, task), mode, sid = taken, "new", str(uuid.uuid4())
        return 0 if dry else start(begin(mode, issue, sid, board.role(issue), task, opts, runs))


def options(argv):
    """The options from argv, or None for a form USAGE doesn't allow."""
    opts = {"dry": False, "now": False, "tui": False, "issue": None, "split": None, "split_from": None, "events": None,
            "manager": None}
    flags = {"--dry-run": "dry", "--now": "now", "--tui": "tui"}
    valued = {"--issue": "issue", "--split": "split", "--split-from": "split_from", "--events": "events",
              "--manager": "manager"}
    i = 0
    while i < len(argv):
        a, value = argv[i], argv[i + 1] if i + 1 < len(argv) else None
        name, eq, inline = a.partition("=")
        if a in flags:
            opts[flags[a]] = True
        elif eq and name in ("--split", "--split-from", "--events", "--manager") and opts[valued[name]] is None:
            opts[valued[name]] = inline
        elif a in valued and opts[valued[a]] is None and value is not None and value not in flags | valued:
            opts[valued[a]] = value
            i += 1
        else:
            return None
        i += 1
    if opts["issue"] is not None and opts["now"]:
        return None
    if not opts["tui"] and any(opts[k] is not None for k in ("split", "split_from", "events", "manager")):
        return None
    return opts


def main(argv, gql=linear_gql, now=None, tdir=PROJECTS, config=None, runs=RUNS_LOG, sh=subprocess.run, hour=None,
         root=ROOT, run=sh_run, keychain=linear.has_key, start=None):
    """start(outer's arguments) starts an agent run; default: outer with these sh, gql, run, tdir, keychain and root."""
    if "--brake" in argv:
        if argv != ["--brake"]:
            print(USAGE, file=sys.stderr)
            return 2
        os.environ["PATH"] = PATH
        ok, summary = probe(sh, BRAKE_5H)
        print(summary)
        return 0 if ok else 1
    opts = options(argv)
    if opts is None:
        print(USAGE, file=sys.stderr)
        return 2
    if opts["issue"] is not None and not re.fullmatch(ISSUE_ID, opts["issue"]):
        print(f"router.py: bad issue id: {opts['issue']}", file=sys.stderr)
        return 2
    os.environ["PATH"] = PATH
    try:
        cfg = load_config(config) if config else load_config()
        runnable(cfg, root)
    except CONFIG_ERRORS as e:
        return config_error("router", e, opts["dry"])
    if opts["tui"]:
        try:
            attended.layout(opts["split"], opts["split_from"])
            if opts["events"] is not None:
                tui_claude.events_file(opts["events"])
            elif (directory := manager.directory(opts["manager"], proc=sh)) is not None:
                opts["events"] = manager.events(directory, create=not opts["dry"])
            else:
                print("router.py: no manager directory (not in tmux): this run writes no events; "
                      "give --manager <name>", file=sys.stderr)
        except (attended.Bad, tui_claude.TuiError, manager.ManagerError) as e:
            print(f"router.py: {e}", file=sys.stderr)
            return 2
    now = now or datetime.now(timezone.utc)
    start = start or functools.partial(outer, sh=sh, gql=gql, run=run, projects=tdir, keychain=keychain, root=root)
    try:
        if opts["issue"]:
            return run_issue(opts, gql, now, cfg, tdir, runs, sh, root, start)
        return tick(opts, gql, now, cfg, tdir, runs, sh, datetime.now().hour if hour is None else hour, root, start)
    except linear.Unavailable as e:
        return linear.linear_error("router", e, opts["dry"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
