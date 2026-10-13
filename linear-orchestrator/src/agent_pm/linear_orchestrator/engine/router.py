#!/usr/bin/env python3
"""Router: the Linear orchestrator's one entry point. Decides which agent run starts or resumes
among the team's issues assigned to role accounts, then starts it through the outer (run.py in a
detached Driver session).

python3 -P -m agent_pm.linear_orchestrator.engine.router [--now] [--dry-run] [--issue ID] | --brake
ROLE

(no mode)     One scheduled tick: the hours check, then at most one agent run. Exits 1 a config
error or Linear unavailable (config-error, linear-error: once a day), else 0. --now       Skip the
01:00-06:59 hours check. --issue ID    Start or resume only this issue, without the hours, max_runs
and usage gates. Exits 1 a config error, Linear unavailable, agent runs unknown, a live agent run of
the issue or nothing started, else the outer's code: 0 started or bounced, 1 the start failed, 2 a
config error of the issue, 3 transient. --dry-run     With either: print the plan (a tick's with its
usage checks); change nothing, start nothing; exit 0. --brake ROLE Print ROLE's runtime usage; exit
0 if a deep-research round may start, 1 if not or unknown, 2 no such role. Bad arguments exit 2.

Events go to orchestrator.jsonl (linear.log, src router); an idle tick writes none, a usage- blocked
one only its usage-skip lines.
"""

import argparse
import contextlib
import fcntl
import functools
import os
import re
import sys
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent_pm.delegate import PYTHONPATH
from agent_pm.linear_orchestrator.engine import config, inputs, target
from agent_pm.linear_orchestrator.engine.runs import limits, note, parse_log, runtime_of, trim
from agent_pm.linear_orchestrator.engine.types import (
    SERVICE_ERRORS,
    ConfigError,
    Invalid,
    LinearError,
    Transient,
    Unavailable,
)
from agent_pm.linear_orchestrator.linear import issues, linear, sessions, writeback
from agent_pm.linear_orchestrator.linear.linear import (
    ISSUE_ID,
    config_error,
    humans,
    linear_error,
    linear_gql,
    log,
    one_line,
    parse_time,
    role_ids,
    task_group,
    team,
)
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError

STALE = timedelta(hours=2)
LIVE = timedelta(minutes=30)
CAP = 4
SKEW = timedelta(minutes=5)
MAX_5H = 90
BRAKE_5H = 80
MAX_WEEK = 100
CAP_COMMENT = "Tried 4 times without finishing; needs a look."
INTERRUPTED = "The previous agent run was interrupted. Moving this issue back to the Todo queue."
BUSY = "another router is running"
USAGE = (
    "usage: python3 -P -m agent_pm.linear_orchestrator.engine.router "
    "[--now] [--dry-run] [--issue ID] | --brake ROLE"
)
FLAGS = {"--dry-run": "dry", "--now": "now"}
RUN = ("-P", "-m", "agent_pm.linear_orchestrator.engine.run")
SESSIONS = "agent-pm-"
SHARED = ("issue", "project", "assignee", "sid", "task", "mode", "runtime")
DONE = {"completed", "canceled", "duplicate"}
UNREADABLE = "(unreadable)"
RELATIONS = "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"
Q_RELATIONS = "query($i: String!) { issue(id: $i) { " + RELATIONS + " } }"
Q_RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } } }"
)
Q_HISTORY = "query($i: String!) { issue(id: $i) { " + linear.HISTORY + " } }"


def latest_sid(entries, issue):
    return next((e.sid for e in reversed(entries) if e.issue == issue), None)


def sid_times(entries, sid):
    return [e.ts for e in entries if e.sid == sid]


def first_line_time(entries, sid):
    return next(
        (e.ts for e in entries if e.sid == sid and e.kind == "start"), sid_times(entries, sid)[0]
    )


def attempt_count(entries, issue, since=None):
    return sum(1 for e in entries if e.issue == issue and (since is None or e.ts > since))


def live_sessions(roles, *, driver):
    try:
        names = [row.name for row in driver.sessions(re.escape(SESSIONS) + ".*")]
    except DriverError:
        return None
    live = {}
    for r in roles:
        prefix = config.session(r, "")
        ids = [n.removeprefix(prefix) for n in names if n.startswith(prefix)]
        live[r] = sorted(i for i in ids if re.fullmatch(ISSUE_ID, i))
    return live


def full_roles(roles, live):
    return {r for r in roles if len(live[r]) >= roles[r].max_runs}


def summary(usage):
    if usage is None:
        return "unknown"
    return " ".join(
        f"{k}={'none' if w is None else f'{w.used_percent:g}%'}"
        for k, w in (("five_hour", usage.five_hour), ("weekly", usage.weekly))
    )


def exhausted(usage, max_5h=MAX_5H):
    five, week = usage.five_hour, usage.weekly
    return (five is not None and five.used_percent >= max_5h) or (
        week is not None and week.used_percent >= MAX_WEEK
    )


def read_usage(runtime, *, driver):
    try:
        return driver.usage(runtime)
    except (SessionConfigError, DriverError, OSError, ValueError, RuntimeError):
        return None


class Gate:
    def __init__(self, runs, now, dry, say=None, *, driver):
        self.driver = driver
        self.limits, self.dry, self.seen = limits(runs, now), dry, {}
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=dry)

    def blocked(self, runtime, ident):
        if runtime not in self.seen:
            self.seen[runtime] = self._check(runtime)
        reason, fields = self.seen[runtime]
        if reason:
            self.say("usage-skip", ident, once=True, runtime=runtime, reason=reason, **fields)
        return reason is not None

    def _check(self, runtime):
        if (at := self.limits.get(runtime)) is not None:
            shown = {"until": at.isoformat()}
            reason, fields = "limit", shown
        else:
            usage = read_usage(runtime, driver=self.driver)
            shown = {"usage": summary(usage)}
            reason = "usage unknown" if usage is None else "usage" if exhausted(usage) else None
            fields = shown if reason == "usage" else {}
        if self.dry:
            self.say("usage", runtime=runtime, **shown, allowed=reason is None)
        return reason, fields


def rank(issue):
    return issue["priority"] or 5  # 0 = no priority = lowest


def blockers(issue):
    nodes = [n for n in issue["inverseRelations"]["nodes"] if n["type"] == "blocks"]
    return [
        n["issue"]["identifier"] if n.get("issue") else UNREADABLE
        for n in nodes
        if not n.get("issue") or n["issue"]["state"]["type"] not in DONE
    ]


def task_for(labels, group, role, role_tasks, label_tasks):
    found = [n for n in labels if n["parent"] and n["parent"]["id"] == group]
    if not found:
        return None, None
    if len(found) > 1:
        return (
            None,
            (
                f"Several task labels ({', '.join(n['name'] for n in found)}"
                f"); keep one, then move the issue back to Todo."
            ),
        )
    name = found[0]["name"]
    task = label_tasks.get(found[0]["id"])
    if task is None:
        return (
            None,
            (
                f'Task label "{name}" is not in {config.LOCAL}'
                f"'s [task_labels]. Fix the label, then move the issue back to Todo."
            ),
        )
    if task not in role_tasks:
        return None, (
            f'Task label "{name}" is not one of {role}\'s tasks ({", ".join(role_tasks)}). '
            "Fix the label or the assignee, then move the issue back to Todo."
        )
    return task, None


def q_issues(fields=""):
    return (
        (
            "query($f: IssueFilter) { issues(filter: $f, first: 100) { nodes { id "
            "identifier url priority createdAt updatedAt project { id name } "
            "assignee { id email } "
        )
        + fields
        + " } } }"
    )


class Board:
    def __init__(self, gql, entries, now, dry, cfg, gate, say=None):
        self.gql, self.entries, self.now, self.dry = gql, entries, now, dry
        self.cfg, self.gate = cfg, gate
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=dry)
        self.hist, self.ready, self.blocked, self.live, self.statuses = {}, None, {}, set(), {}
        self.label_tasks = {i: task for task, i in cfg.task_labels.items()}
        t = team(gql, cfg)
        task_group(gql, cfg)
        self.team, self.states = t.id, t.states
        self.humans = set(humans(gql, cfg))
        self.roles = role_ids(gql, cfg.roles)

    def issues(self, state, fields=""):
        flt = {
            "team": {"id": {"eq": self.team}},
            "project": {"null": False},
            "assignee": {"id": {"in": list(self.roles)}},
            "state": {"id": {"eq": self.states[state]}},
        }
        return self.gql(q_issues(fields), f=flt)["issues"]["nodes"]

    def todo(self):
        if self.ready is None:
            try:
                issues = self.issues("todo", RELATIONS)
            except LinearError as e:
                self.say("todo-error", error=one_line(e))
                issues = self.issues("todo")
                for i in issues:
                    try:
                        i.update(self.gql(Q_RELATIONS, i=i["id"])["issue"])
                    except LinearError:
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
        return issue["identifier"] not in self.live and self.role(issue) not in full

    def later(self, issue):
        return -self.cfg.stages.get(self.role(issue), 0)

    def last_move(self, issue, state, by_user=False):
        if issue["id"] not in self.hist:
            self.hist[issue["id"]] = self.gql(Q_HISTORY, i=issue["id"])["issue"]["history"]["nodes"]
        return linear.last_move(
            self.hist[issue["id"]], {self.states[state]}, self.humans if by_user else None
        )

    def attempts(self, issue):
        n = attempt_count(self.entries, issue["identifier"])
        return (
            n
            if n < CAP
            else attempt_count(
                self.entries, issue["identifier"], self.last_move(issue, "todo", by_user=True)
            )
        )

    def current_sid(self, issue):
        sid = latest_sid(self.entries, issue["identifier"])
        if not sid:
            return None
        moved = self.last_move(issue, "in_progress")
        return sid if moved is None or first_line_time(self.entries, sid) >= moved - SKEW else None

    def comment_and_move(self, issue, body, state, frm, step):
        if self.dry:
            return
        left = linear.comment_and_move(
            self.gql,
            issue["id"],
            body,
            self.states[state],
            self.states[frm],
            self.cfg.human_members if state == "in_review" else (),
        )
        if left is not None:
            self.say("move-skip", issue["identifier"], step=step, state=left)

    def status(self, ident, sid):
        if sid not in self.statuses:
            try:
                st = sessions.status(
                    self.cfg.run_dir(ident),
                    runtime_of(self.entries, sid),
                    sid,
                    driver=self.cfg.driver,
                )
                self.statuses[sid] = None if st.resumable == "unknown" else st
            except (SessionConfigError, DriverError, OSError, ValueError, RuntimeError):
                self.statuses[sid] = None
        return self.statuses[sid]

    def unknown(self, ident, sid):
        self.say("recover-skip", ident, once=True, sid=sid, reason="session unknown")
        return False

    def recover(self, live_ids=()):
        self.live = set(live_ids)
        mine = [
            (i, self.current_sid(i))
            for i in self.issues("in_progress")
            if i["identifier"] not in self.live
        ]
        mine.sort(
            key=lambda p: (
                p[1] is None,
                rank(p[0]),
                self.later(p[0]),
                first_line_time(self.entries, p[1]) if p[1] else self.now,
            )
        )
        return [(issue, sid) for issue, sid in mine if self.settle(issue, sid)]

    def settle(self, issue, sid, wait=True):
        ident = issue["identifier"]
        if wait and (latest := latest_sid(self.entries, ident)):
            st = self.status(ident, latest)
            if st is None:
                return self.unknown(ident, latest)
            if st.last_active is not None and st.last_active > (self.now - LIVE).timestamp():
                return False
        if sid and self.attempts(issue) >= CAP:
            self.say("recover", ident, to="in_review", reason=f"reached {CAP} attempts")
            self.comment_and_move(issue, CAP_COMMENT, "in_review", "in_progress", "recover")
        elif sid:
            st = self.status(ident, sid)
            if st is None:
                return self.unknown(ident, sid)
            if st.resumable == "yes":
                role, logged = (
                    self.role(issue),
                    next(e.role for e in reversed(self.entries) if e.sid == sid),
                )
                if logged == role:
                    return True
                why = f"role ({logged}) is not the assignee's ({role})"
                self.say("recover", ident, to="in_review", reason=why)
                self.comment_and_move(
                    issue,
                    f"The interrupted agent run's {why}; needs a look.",
                    "in_review",
                    "in_progress",
                    "recover",
                )
            elif not wait or sid_times(self.entries, sid)[-1] < self.now - LIVE:
                self.say("recover", ident, to="todo", reason="not resumable", sid=sid)
                self.comment_and_move(issue, INTERRUPTED, "todo", "in_progress", "recover")
        elif not wait or parse_time(issue["updatedAt"]) < self.now - STALE:
            self.say("recover", ident, to="todo", reason="no session", updated=issue["updatedAt"])
            self.comment_and_move(issue, INTERRUPTED, "todo", "in_progress", "recover")
        return False

    def queue(self, full=()):
        return sorted(
            (i for i in self.todo() if self.available(i, full)),
            key=lambda i: (rank(i), self.later(i), i["createdAt"]),
        )

    def plan(self, run, live=None):
        if run[0] == "resume":
            self.say("plan", run[1]["identifier"], mode="resume", sid=run[2], live=live)
        else:
            self.say("plan", mode="new", queue=run[1], live=live)

    def gated(self, runtime, ident):
        return self.gate is not None and self.gate.blocked(runtime, ident)

    def take(self, only=None, full=()):
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
                role = self.role(issue)
                runtime = self.cfg.roles[role].run.runtime
                if not self.gated(runtime, ident):
                    return issue, None, role, runtime
            elif taken := self.claim(issue, full):
                return taken
        if queue:
            self.say("pick-none", reason="nothing claimable")
        else:
            self.say(
                "pick-none",
                only,
                reason="not a Todo issue assigned to a role account" if only else "queue empty",
            )
        return None

    def claim(self, issue, full):
        ident = issue["identifier"]
        # re-check right before claiming so a concurrent change isn't overwritten
        current = self.gql(Q_RECHECK, i=issue["id"])["issue"]
        role = config.role_for(self.cfg.roles, (current["assignee"] or {}).get("email"))
        if current["state"]["id"] != self.states["todo"]:
            skip = "no longer Todo"
        elif role is None:
            skip = "not assigned to a role"
        elif role in full:
            skip = f"{role} is full"
        elif current["project"] is None:
            skip = "no project"
        else:
            skip = None
        if skip:
            self.say("claim-skip", ident, reason=skip)
            return None
        task, comment = task_for(
            current["labels"]["nodes"],
            self.cfg.task_label_group,
            role,
            self.cfg.roles[role].tasks,
            self.label_tasks,
        )
        if comment:
            self.say("claim-skip", ident, to="in_review", reason="bad task label")
            self.comment_and_move(issue, comment, "in_review", "todo", "claim")
            return None
        runtime = self.cfg.roles[role].run.runtime
        if self.gated(runtime, ident):
            return None
        self.say("claim", ident, role=role, task=task, runtime=runtime)
        linear.call(
            self.gql, linear.M_STATE, "issueUpdate", i=issue["id"], s=self.states["in_progress"]
        )
        taken = {**issue, "assignee": current["assignee"], "project": current["project"]}
        return taken, task, role, runtime


def begin(mode, issue, sid, role, task, runtime, runs):
    ident = issue["identifier"]
    kind = "resume" if mode == "resume" else "start"
    note(runs, kind, issue=ident, sid=sid, role=role, runtime=runtime)
    return argparse.Namespace(
        issue=ident,
        project=issue["project"]["id"],
        assignee=issue["assignee"]["email"],
        sid=sid,
        task=task,
        mode=mode,
        runtime=runtime,
    )


def fail(ident, kind, reason, rc):
    log("router", kind, ident, reason=reason)
    return rc


def outer(a, cfg, *, gql, run, keychain):
    """Launch with canonical import roots followed by caller-provided Python paths."""
    os.environ["PATH"] = config.path()
    if not re.fullmatch(ISSUE_ID, a.issue) or not config.UUID_RE.fullmatch(a.sid):
        print(f"router.py: bad issue or session id: {a.issue} {a.sid}", file=sys.stderr)
        return 2
    role = config.role_for(cfg.roles, a.assignee)
    if role is None:
        return fail(a.issue, "config-error", f"{a.assignee!r} is not a role account", 2)
    tasks, key = cfg.roles[role].tasks, cfg.roles[role].key
    if a.task is not None and a.task not in tasks:
        why = f"task {a.task!r} is not one of {role}'s tasks ({', '.join(tasks)})"
        return fail(a.issue, "config-error", why, 2)
    if not keychain(key):
        return fail(a.issue, "config-error", f"no Keychain item for role key {key}", 2)
    if a.mode == "resume":
        try:
            st = sessions.status(cfg.run_dir(a.issue), a.runtime, a.sid, driver=cfg.driver)
            why = None if st.resumable == "yes" else st.resumable
        except (SessionConfigError, DriverError, OSError, ValueError, RuntimeError) as e:
            why = one_line(e)
        if why:
            return fail(a.issue, "transient", f"session {a.sid} not resumable ({why})", 3)
    try:
        issue = issues.read_issue(gql, a.issue)
    except SERVICE_ERRORS as e:
        return fail(a.issue, "transient", f"Linear: {one_line(e)}", 3)
    ctx = writeback.context(
        cfg,
        role,
        gql,
        ident=a.issue,
        issue_id=issue.id,
        sid=a.sid,
        resume=a.mode == "resume",
        project=a.project,
        target=None,
    )
    build = config.TASKS[role].kind == "build"
    if build:
        try:
            repo = target.check(
                issue, cfg.project_repos, run=run, work=cfg.runs_dir, roles=cfg.roles
            )
        except SERVICE_ERRORS as e:
            return fail(a.issue, "transient", f"repo check: {one_line(e)}", 3)
        if isinstance(repo, Transient) or isinstance(repo, Invalid) and a.mode == "resume":
            return fail(a.issue, "transient", repo.reason, 3)  # never bounce a mid-build agent run
        if isinstance(repo, Invalid):
            try:
                writeback.bounce(ctx, issue, repo.reason)
            except SERVICE_ERRORS as e:
                return fail(a.issue, "transient", f"bounce: {one_line(e)}", 3)
            log("router", "bounce", a.issue, reason=repo.reason)
            return 0
    else:
        repo = target.research_repo(issue, cfg.project_repos)
    if repo is not None:
        repo = target.with_clone(repo, a.issue, cfg.local_clones, work=cfg.runs_dir, run=run)
    rd = cfg.run_dir(a.issue)
    try:
        sources = inputs.gather(issue, role, cfg.docs, run=run)
    except SERVICE_ERRORS as e:
        return fail(a.issue, "transient", f"docs: {one_line(e)}", 3)
    try:
        os.makedirs(rd, exist_ok=True)
        text = inputs.render(
            issue, role, sources, humans=ctx.humans, target=repo, docs=cfg.docs
        ).replace("\0", "")
        text = text.encode("utf-8", "replace").decode()  # a lone surrogate would fail execve
    except SERVICE_ERRORS as e:
        log("router", "launch-error", a.issue, error=f"input: {one_line(e)}")
        return 1
    try:
        cfg.driver.detach(
            config.session(role, a.issue),
            [
                sys.executable,
                *RUN,
                "--uuid",
                issue.id,
                *(["--target", f"{repo.owner}/{repo.name}"] if build and repo is not None else []),
                *(f"--{k}={v}" for k in SHARED if (v := getattr(a, k)) is not None),
                f"--input={text}",
            ],
            cwd=rd,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    dict.fromkeys(
                        os.path.abspath(path)
                        for path in (
                            *PYTHONPATH.split(os.pathsep),
                            *os.environ.get("PYTHONPATH", "").split(os.pathsep),
                        )
                        if path
                    )
                ),
            },
        )
    except (SessionConfigError, DriverError) as e:
        log("router", "launch-error", a.issue, error=one_line(e))
        return 1
    return 0


@contextlib.contextmanager
def lock(runs, dry):
    if dry:
        yield True
        return
    os.makedirs(os.path.dirname(runs), exist_ok=True)
    with open(
        os.path.join(os.path.dirname(runs), "router.lock"), "a"
    ) as f:  # not inherited by children (PEP 446)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free = True
        except BlockingIOError:
            free = False
        yield free


class Held:
    """A tick's walk log: plan, pick, usage-skip, pick-none and skip lines wait, written before the
    next other line. At the walk's end, waiting usage-skips mean a usage-blocked walk: only they are
    written; else all are. A dry run and an exception wait for nothing.
    """

    WAIT = {"plan", "pick", "usage-skip", "pick-none", "skip"}

    def __init__(self, dry):
        self.dry, self.waiting = dry, []

    def __call__(self, kind, issue=None, **fields):
        if not self.dry and kind in self.WAIT:
            self.waiting.append((kind, issue, fields))
            return
        self.write()
        log("router", kind, issue, dry=self.dry, **fields)

    def write(self, kinds=WAIT):
        for kind, issue, fields in self.waiting:
            if kind in kinds:
                log("router", kind, issue, **fields)
        self.waiting = []

    def __enter__(self):
        return self

    def __exit__(self, error, *_):
        blocked = error is None and any(w[0] == "usage-skip" for w in self.waiting)
        self.write({"usage-skip"} if blocked else self.WAIT)


def choose(board, gate, live_ids, full, counts):
    for issue, sid in board.recover(live_ids):
        if not board.available(issue, full):
            continue
        runtime = runtime_of(board.entries, sid)
        if not gate.blocked(runtime, issue["identifier"]):
            board.plan(("resume", issue, sid), counts)
            return "resume", issue, sid, board.role(issue), None, runtime
    if not (queue := board.queue(full)):
        return None
    board.plan(("new", len(queue)), counts)
    if not (taken := board.take(full=full)):
        board.say("skip", reason="nothing claimed")
        return None
    issue, task, role, runtime = taken
    return "new", issue, str(uuid.uuid4()), role, task, runtime


def tick(opts, cfg, gql, now, hour, start):
    dry, runs = opts["dry"], cfg.runs_log
    say = functools.partial(log, "router", dry=dry)
    if not opts["now"] and not 1 <= hour <= 6:
        if not dry:
            return 0
        say("skip", reason="outside hours")
    with lock(runs, dry) as free:
        if not free:
            say("skip", reason=BUSY)
            return 0
        live = live_sessions(cfg.roles, driver=cfg.driver)
        if live is None:
            say("skip", reason="agent runs unknown")
            return 0
        full = full_roles(cfg.roles, live)
        counts = {
            r: f"{len(ids)}/{cfg.roles[r].max_runs}" for r, ids in sorted(live.items()) if ids
        } or None
        if len(full) == len(cfg.roles):
            if dry:
                say("skip", reason="all roles full", live=counts)
            return 0
        if not dry:
            try:
                trim(runs, now)
            except SERVICE_ERRORS as e:
                say("skip", reason="prune failed", error=one_line(e))
        with Held(dry) as walk:
            gate = Gate(runs, now, dry, walk, driver=cfg.driver)
            board = Board(gql, parse_log(runs), now, dry, cfg, gate, walk)
            run = choose(board, gate, {i for ids in live.values() for i in ids}, full, counts)
            if run is None or dry:
                return 0
            a = begin(*run, runs)
            walk.write()  # the outer logs past the walk
            try:
                rc = start(a)
            except SERVICE_ERRORS as e:
                walk("launch-error", a.issue, error=one_line(e))
                return 0
            walk("launch", a.issue, exit=rc)
            return 0


def run_issue(opts, cfg, gql, now, start):
    ident, dry, runs = opts["issue"], opts["dry"], cfg.runs_log
    stop = 0 if dry else 1
    with lock(runs, dry) as free:
        if not free:
            log("router", "skip", ident, reason=BUSY)
            print(f"router.py: {BUSY}; try again", file=sys.stderr)
            return stop
        live = live_sessions(cfg.roles, driver=cfg.driver)
        if live is None:
            print("router.py: agent runs unknown; try again", file=sys.stderr)
            return stop
        if role := next((r for r, ids in live.items() if ident in ids), None):
            attach = cfg.driver.attach_command(config.session(role, ident))
            print(f"router.py: {ident} has a live agent run: {attach}", file=sys.stderr)
            return stop
        entries = parse_log(runs)
        board = Board(gql, entries, now, dry, cfg, None)
        if issue := next(
            (i for i in board.issues("in_progress") if i["identifier"] == ident), None
        ):
            sid = board.current_sid(issue)
            if not board.settle(issue, sid, wait=False):
                return stop
            board.plan(("resume", issue, sid))
            run = ("resume", issue, sid, board.role(issue), None, runtime_of(entries, sid))
        elif board.is_blocked(ident) or not (taken := board.take(ident)):
            return stop
        else:
            issue, task, role, runtime = taken
            run = ("new", issue, str(uuid.uuid4()), role, task, runtime)
        return 0 if dry else start(begin(*run, runs))


def brake(role, cfg):
    usage = read_usage(cfg.roles[role].run.runtime, driver=cfg.driver)
    print(summary(usage))
    return 1 if usage is None or exhausted(usage, BRAKE_5H) else 0


def options(argv):
    if "--brake" in argv:
        ok = len(argv) == 2 and argv[0] == "--brake" and not argv[1].startswith("-")
        return {"brake": argv[1], "dry": False, "now": False, "issue": None} if ok else None
    opts = {"brake": None, "dry": False, "now": False, "issue": None}
    i = 0
    while i < len(argv):
        a, value = argv[i], argv[i + 1] if i + 1 < len(argv) else None
        if a in FLAGS:
            opts[FLAGS[a]] = True
        elif a == "--issue" and opts["issue"] is None and value not in (None, "--issue", *FLAGS):
            opts["issue"] = value
            i += 1
        else:
            return None
        i += 1
    return None if opts["issue"] is not None and opts["now"] else opts


def main(
    argv,
    *,
    gql=linear_gql,
    now=None,
    config_path=config.CONFIG,
    hour=None,
    run=config.sh_run,
    keychain=linear.has_key,
    start=None,
    driver=None,
):
    opts = options(argv)
    if opts is None:
        print(USAGE, file=sys.stderr)
        return 2
    if opts["issue"] is not None and not re.fullmatch(ISSUE_ID, opts["issue"]):
        print(f"router.py: bad issue id: {opts['issue']}", file=sys.stderr)
        return 2
    os.environ["PATH"] = config.path()
    try:
        cfg = config.load(path=config_path)
    except ConfigError as e:
        if not e.logs_dir:
            print(e.msg, file=sys.stderr)
            return 1
        linear.use_logs(e.logs_dir)
        return config_error("router", e, opts["dry"])
    cfg = replace(cfg, driver=driver) if driver is not None else cfg
    linear.use_logs(cfg.logs_dir)
    if opts["brake"] is not None:
        if opts["brake"] not in cfg.roles:
            print(f"router.py: no role {opts['brake']}", file=sys.stderr)
            return 2
        return brake(opts["brake"], cfg)
    gql = functools.partial(gql, service=cfg.harness_key)
    start = start or functools.partial(outer, cfg=cfg, gql=gql, run=run, keychain=keychain)
    now = now or datetime.now(UTC)
    try:
        if opts["issue"]:
            return run_issue(opts, cfg, gql, now, start)
        return tick(opts, cfg, gql, now, datetime.now().hour if hour is None else hour, start)
    except Unavailable as e:
        return linear_error("router", e, opts["dry"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
