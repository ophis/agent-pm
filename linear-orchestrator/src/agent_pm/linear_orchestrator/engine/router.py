"""Router: the Linear orchestrator's scheduler. Decides which agent run starts or resumes among
the team's issues assigned to role accounts, then only triggers it: outer prepares its input, writes
its runs.jsonl row and detaches its LinearRun, whose Delegate's harness calls `agent-pm-linear
writeback` on each event; the router exits while the run goes on.

agent-pm-linear router [--now] [--dry-run] [--issue ID] [--tui [--display D] [--events FILE]] | --brake ROLE

(no mode)     One scheduled tick: the hours check, then at most one agent run. Exits 1 a config error or Linear
              unavailable (config-error, linear-error: once a day), else 0.
  --now       Skip the 01:00-06:59 hours check.
--issue ID    Start or resume only this issue, without the hours, max_runs and usage gates. Exits 1 a config error,
              Linear unavailable, agent runs unknown, a live agent run of the issue or nothing started, 0 its ended
              run's outcome written back instead, else the outer's code: 0 detached or bounced, 1 the launch
              failed, 2 a config error of the issue, 3 transient.
--dry-run     With either: print the plan (a tick's with its usage checks); change nothing, start nothing; exit 0.
--tui         With either: each run it starts or resumes is attended (tui mode), shown at D (opaque, its format:
              agent-pm-session --help; none: the caller's place), its events written to FILE, else
              $AGENT_PM_EVENTS. Exits 2 before any Linear call without either, or when the Delegate refuses D or
              FILE (no place to show the TUI, a D its Host cannot use, FILE not writable). A run it starts or
              resumes prints its Record line on stdout, its sessions and attach command on stderr. launchd
              never passes it: a tick resumes a run headless.
--brake ROLE  Print ROLE's client usage; exit 0 if a deep-research round may start, 1 if not or unknown, 2 no such
              role.
--help        Print this; exit 0. Bad arguments exit 2.

Events go to orchestrator.jsonl (event_log.log, src router); an idle tick writes none, a usage-blocked
one only its usage-skip lines."""

import contextlib
import fcntl
import functools
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from traceback import format_exception

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import (
    config,
    inputs,
    issues,
    ops,
    target,
    writeback,
)
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.client import has_key, linear_gql
from agent_pm.linear_orchestrator.engine.gate import BRAKE_5H, Gate, exhausted, read_usage, summary
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.log import linear_error, log
from agent_pm.linear_orchestrator.engine.ops import humans, role_ids, task_group, team
from agent_pm.linear_orchestrator.engine.runs import (
    client_of,
    delegate_of,
    failures,
    latest,
    note,
    parse_log,
    trim,
)
from agent_pm.linear_orchestrator.engine.types import (
    ISSUE_ID,
    ConfigError,
    InputError,
    Invalid,
    IssueError,
    LinearError,
    Spec,
    Transient,
    Unavailable,
)
from agent_pm.utils import one_line, parse_time

STALE = timedelta(hours=2)
LIVE = timedelta(minutes=30)
CAP = 4
SKEW = timedelta(minutes=5)
CAP_COMMENT = "Tried 4 times without finishing; needs a look."
INTERRUPTED = "The previous agent run was interrupted. Moving this issue back to the Todo queue."
BUSY = "another router is running"
USAGE = (
    "usage: agent-pm-linear router [--now] [--dry-run] [--issue ID] "
    "[--tui [--display D] [--events FILE]] | --brake ROLE"
)
FLAGS = {"--dry-run": "dry", "--now": "now", "--tui": "tui"}
VALUED = {"--issue": "issue", "--display": "display", "--events": "events"}
EVENTS_ENV = "AGENT_PM_EVENTS"
DONE = {"completed", "canceled", "duplicate"}
UNREADABLE = "(unreadable)"
RELATIONS = "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"
Q_RELATIONS = "query($i: String!) { issue(id: $i) { " + RELATIONS + " } }"
Q_RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } } }"
)
Q_HISTORY = "query($i: String!) { issue(id: $i) { " + ops.HISTORY + " } }"


@dataclass(frozen=True)
class Attended:
    """A --tui router's runs: shown at display (passed on as given), events (an absolute path)."""

    display: str | None
    events: str


def latest_sid(entries, issue):
    entry = latest(entries).get(issue)
    return None if entry is None else entry.sid


def sid_times(entries, sid):
    return [e.ts for e in entries if e.sid == sid]


def first_line_time(entries, sid):
    return next(
        (e.ts for e in entries if e.sid == sid and e.kind == "start"), sid_times(entries, sid)[0]
    )


def attempt_count(entries, issue, since=None):
    return sum(1 for e in entries if e.issue == issue and (since is None or e.ts > since))


def live_sessions(cfg, entries, *, say):
    """A run whose view cannot load counts as not alive, logged once; inventory unknown: None."""
    live = {role: [] for role in cfg.roles}
    for ident, entry in sorted(latest(entries).items()):
        if entry.role not in live:
            continue
        try:
            if LinearRun.attach(ident, cfg=cfg).alive():
                live[entry.role].append(ident)
        except (ConfigError, delegate.ConfigError) as error:
            say("recover-skip", ident, once=True, sid=entry.sid, reason=one_line(error))
        except delegate.SessionError:
            return None
    return live


def full_roles(roles, live):
    return {r for r in roles if len(live[r]) >= roles[r].max_runs}


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
                f"Several task labels ({', '.join(n['name'] for n in found)}); keep one, then move the issue back to Todo."
            ),
        )
    name = found[0]["name"]
    task = label_tasks.get(found[0]["id"])
    if task is None:
        return (
            None,
            (
                f'Task label "{name}" is not in {config.LOCAL}\'s [task_labels]. Fix the label, then move the issue back to Todo.'
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
            "query($f: IssueFilter) { issues(filter: $f, first: 100) { nodes { id identifier url priority createdAt "
            "updatedAt project { id name } assignee { id email } "
        )
        + fields
        + " } } }"
    )


class Board:
    def __init__(self, gql, entries, cfg, *, now, dry, gate=None, say=None):
        self.gql, self.entries, self.now, self.dry = gql, entries, now, dry
        self.cfg, self.gate = cfg, gate
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=dry)
        self.hist, self.ready, self.blocked, self.live, self.statuses = {}, None, {}, set(), {}
        self.status_errors, self.replays = {}, {}
        self.failures = failures(cfg.runs_log)
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

    def last_move(self, issue, state, *, by_user=False):
        if issue["id"] not in self.hist:
            self.hist[issue["id"]] = self.gql(Q_HISTORY, i=issue["id"])["issue"]["history"]["nodes"]
        return ops.last_move(
            self.hist[issue["id"]], {self.states[state]}, self.humans if by_user else None
        )

    def attempts(self, issue):
        tried = [*self.entries, *self.failures]
        n = attempt_count(tried, issue["identifier"])
        return (
            n
            if n < CAP
            else attempt_count(
                tried, issue["identifier"], self.last_move(issue, "todo", by_user=True)
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
        left = ops.comment_and_move(
            self.gql,
            issue["id"],
            body,
            to=self.states[state],
            frm=self.states[frm],
            emails=self.cfg.human_members if state == "in_review" else (),
        )
        if left is not None:
            self.say("move-skip", issue["identifier"], step=step, state=left)

    def status(self, ident, sid):
        if sid not in self.statuses:
            try:
                st = LinearRun.attach(ident, cfg=self.cfg, sid=sid).session_info()
                self.statuses[sid] = None if st.resumable == "unknown" else st
            except ConfigError as error:
                self.status_errors[sid] = one_line(error)
                self.statuses[sid] = None
            except (delegate.ConfigError, delegate.SessionError):
                self.statuses[sid] = None
        return self.statuses[sid]

    def unknown(self, ident, sid):
        self.say(
            "recover-skip",
            ident,
            once=True,
            sid=sid,
            reason=self.status_errors.get(sid, "session unknown"),
        )
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

    def recent_session(self, ident):
        latest = latest_sid(self.entries, ident)
        if latest is None:
            return False
        status = self.status(ident, latest)
        if status is None:
            self.unknown(ident, latest)
            return True
        return status.last_active is not None and status.last_active > (self.now - LIVE).timestamp()

    def replay(self, issue, sid):
        """The ended run's outcome its ledger has not completed, written back in process instead of
        a resume (FR-41): None when there is none; else whether its write-back is now complete.
        """
        ident = issue["identifier"]
        try:
            outcome = LinearRun.attach(ident, cfg=self.cfg, sid=sid).outcome()
        except (ConfigError, delegate.ConfigError, delegate.SessionError):
            return None  # status says why
        if outcome is None:
            return None
        try:
            done = writeback.replay(self.cfg, ident, sid, outcome, gql=self.gql, dry=self.dry)
        except (ConfigError, ValueError) as error:
            self.say("recover-skip", ident, once=True, sid=sid, reason=one_line(error))
            return False
        if done is not None:
            self.say("recover", ident, to="writeback", sid=sid)
        return done

    def settle(self, issue, sid, *, wait=True):
        ident = issue["identifier"]
        if wait and self.recent_session(ident):
            return False
        if sid and (replayed := self.replay(issue, sid)) is not None:
            self.replays[ident] = replayed
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

    def gated(self, role, ident):
        return self.gate is not None and self.gate.blocked(role, ident)

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
                client = self.cfg.roles[role].run.client
                if not self.gated(role, ident):
                    return issue, None, role, client
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
        client = self.cfg.roles[role].run.client
        if self.gated(role, ident):
            return None
        self.say("claim", ident, role=role, task=task, client=client)
        ops.call(self.gql, ops.M_STATE, "issueUpdate", i=issue["id"], s=self.states["in_progress"])
        taken = {**issue, "assignee": current["assignee"], "project": current["project"]}
        return taken, task, role, client


def begin(cfg, spec: Spec) -> Spec:
    """A new run of an issue with a recorded run closes that run first (its retained TUI)."""
    if spec.mode == "new" and spec.issue in latest(parse_log(cfg.runs_log)):
        try:
            LinearRun.attach(spec.issue, cfg=cfg).close()
        except (ConfigError, delegate.ConfigError, delegate.SessionError) as error:
            log("router", "tui-error", spec.issue, msg=one_line(error))
        else:
            log("router", "tui-closed", spec.issue)
    return spec


def record(cfg, spec: Spec, *, role, now, target=None) -> str:
    """The run's runs.jsonl row, an attempt toward CAP: a start row holds the claim's context (issue
    uuid, project, target, the role's key), which its write-back reads; returns its Delegate kind.
    """
    if spec.mode == "new":
        kind, claim = (
            cfg.delegate_kind,
            {
                "uuid": spec.uuid,
                "project": spec.project,
                "target": target,
                "key": cfg.roles[role].key,
            },
        )
    else:
        kind, claim = delegate_of(parse_log(cfg.runs_log), spec.sid), {}
    note(
        cfg.runs_log,
        "start" if spec.mode == "new" else "resume",
        issue=spec.issue,
        sid=spec.sid,
        role=role,
        client=spec.client,
        delegate_kind=kind,
        now=now,
        **claim,
    )
    return kind


def fail(ident, kind, reason, rc):
    log("router", kind, ident, reason=reason)
    return rc


def outer(a, cfg, *, gql, run, keychain, now=datetime.now, attended: Attended | None = None):
    """Prepares the issue's input, then detaches its LinearRun, attended (tui mode) when given.
    Its runs.jsonl row, an attempt toward CAP, comes once Recover can read the run back: a resume's
    first, a new run's once its LinearRun (its Delegate's state) is built, before its detach; a new
    run failing before then leaves a `failed` row, an attempt that Recover cannot read back.
    """
    rc = None
    try:
        rc = _outer(a, cfg, gql=gql, run=run, keychain=keychain, now=now, attended=attended)
        return rc
    finally:
        if a.mode == "new" and rc != 0 and a.sid not in {e.sid for e in parse_log(cfg.runs_log)}:
            note(cfg.runs_log, "failed", issue=a.issue, sid=a.sid, now=now)


def _outer(a, cfg, *, gql, run, keychain, now, attended):
    os.environ["PATH"] = config.path()
    if reason := a.identity_error():
        print(f"router.py: {reason}", file=sys.stderr)
        return 2
    role = config.role_for(cfg.roles, a.assignee)
    tasks = cfg.roles[role].tasks if role is not None else ()
    reason = a.role_error(role, tasks)
    if role is None or reason:
        return fail(a.issue, "config-error", reason, 2)
    key = cfg.roles[role].key
    kind = record(cfg, a, role=role, now=now) if a.mode == "resume" else cfg.delegate_kind
    if not keychain(key):
        return fail(a.issue, "config-error", f"no Keychain item for role key {key}", 2)
    if a.mode == "resume":
        try:
            st = LinearRun.attach(a.issue, cfg=cfg, sid=a.sid).session_info()
            why = None if st.resumable == "yes" else st.resumable
        except (ConfigError, delegate.ConfigError, delegate.SessionError) as e:
            why = one_line(e)
        if why:
            return fail(a.issue, "transient", f"session {a.sid} not resumable ({why})", 3)
    build = config.TASKS[role].kind == "build"
    try:
        issue = issues.read_issue(gql, a.issue)
    except (LinearError, Unavailable, OSError, subprocess.SubprocessError, IssueError) as e:
        return fail(a.issue, "transient", f"Linear: {one_line(e)}", 3)
    a = replace(a, uuid=issue.id)
    ctx = writeback.for_run(cfg, role, gql, a)
    if build:
        try:
            repo = target.check(
                issue, cfg.project_repos, run=run, work=cfg.runs_dir, roles=cfg.roles
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return fail(a.issue, "transient", f"repo check: {one_line(e)}", 3)
        if isinstance(repo, Transient) or (isinstance(repo, Invalid) and a.mode == "resume"):
            return fail(a.issue, "transient", repo.reason, 3)  # never bounce a mid-build agent run
        if isinstance(repo, Invalid):
            try:
                writeback.bounce(ctx, issue, repo)
            except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as e:
                return fail(a.issue, "transient", f"bounce: {one_line(e)}", 3)
            log("router", "bounce", a.issue, reason=repo.reason)
            return 0
    else:
        repo = target.research_repo(issue, cfg.project_repos)
    slug = f"{repo.owner}/{repo.name}" if build and repo else None
    if repo is not None:
        repo = target.with_clone(
            repo,
            a.issue,
            clones=cfg.local_clones,
            work=cfg.runs_dir,
            run=run,
        )
    rd = cfg.run_dir(a.issue)
    try:
        sources = inputs.gather(issue, role, cfg.docs, run=run)
    except (InputError, OSError, subprocess.TimeoutExpired) as e:
        return fail(a.issue, "transient", f"docs: {one_line(e)}", 3)
    try:
        os.makedirs(rd, exist_ok=True)
        text = inputs.render(
            issue,
            role,
            sources,
            humans=ctx.humans,
            target=repo,
            docs=cfg.docs,
        ).replace("\x00", "")
        # a lone surrogate cannot be encoded
        text = text.encode("utf-8", "replace").decode()
    except OSError as e:
        log("router", "launch-error", a.issue, error=f"input: {one_line(e)}")
        return 1
    try:
        linear_run = LinearRun(
            a.issue,
            text,
            rd,
            cfg=cfg,
            role=role,
            sid=a.sid,
            task=a.task,
            client=a.client,
            resume=a.mode == "resume",
            delegate_kind=kind,
            **(
                {"mode": "tui", "display": attended.display, "events": attended.events}
                if attended
                else {}
            ),
        )
        if a.mode == "new":
            record(cfg, a, role=role, target=slug, now=now)
        detached = linear_run.detach()
    except (ConfigError, delegate.ConfigError, delegate.SessionError) as e:
        log("router", "launch-error", a.issue, error=one_line(e))
        return 1
    if attended:
        announce(a.issue, linear_run, detached)
    return 0


def announce(ident, linear_run, detached):
    """An attended run's Record line on stdout, its sessions and attach command on stderr."""
    print(detached.to_line(), flush=True)
    print(f"router.py: {ident}: sessions {' '.join(detached.sessions)}", file=sys.stderr)
    try:
        command = linear_run.attach_command()
    except delegate.SessionError as e:
        command = f"unknown ({one_line(e)})"
    if command:
        print(f"router.py: {ident}: attach: {command}", file=sys.stderr)


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
        client = client_of(board.entries, sid)
        if not gate.blocked(
            board.role(issue), issue["identifier"], key=(delegate_of(board.entries, sid), client)
        ):
            board.plan(("resume", issue, sid), counts)
            return Spec.from_issue(issue, mode="resume", sid=sid, task=None, client=client)
    if not (queue := board.queue(full)):
        return None
    board.plan(("new", len(queue)), counts)
    if not (taken := board.take(full=full)):
        board.say("skip", reason="nothing claimed")
        return None
    issue, task, _, client = taken
    return Spec.from_issue(issue, mode="new", sid=str(uuid.uuid4()), task=task, client=client)


def tick(opts, cfg, gql, *, start, clock=datetime.now, hour=None):
    now = clock().astimezone(UTC)
    hour = now.astimezone().hour if hour is None else hour
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
        entries = parse_log(runs)
        live = live_sessions(cfg, entries, say=say)
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
            except (OSError, UnicodeDecodeError) as e:
                say("skip", reason="trim failed", error=one_line(e))
        with Held(dry) as walk:
            gate = Gate(runs, now, dry=dry, say=walk, cfg=cfg)
            board = Board(gql, entries, cfg, now=now, dry=dry, gate=gate, say=walk)
            spec = choose(board, gate, {i for ids in live.values() for i in ids}, full, counts)
            if spec is None or dry:
                return 0
            walk.write()  # the outer logs past the walk
            return start_run(start, begin(cfg, spec), walk)


def run_issue(opts, cfg, gql, start, *, clock=datetime.now):
    now = clock().astimezone(UTC)
    ident, dry, runs = opts["issue"], opts["dry"], cfg.runs_log
    stop = 0 if dry else 1
    with lock(runs, dry) as free:
        if not free:
            log("router", "skip", ident, reason=BUSY)
            print(f"router.py: {BUSY}; try again", file=sys.stderr)
            return stop
        entries = parse_log(runs)
        live = live_sessions(cfg, entries, say=functools.partial(log, "router", dry=dry))
        if live is None:
            print("router.py: agent runs unknown; try again", file=sys.stderr)
            return stop
        if any(ident in ids for ids in live.values()):
            attach = LinearRun.attach(ident, cfg=cfg).attach_command()
            print(f"router.py: {ident} has a live agent run: {attach}", file=sys.stderr)
            return stop
        entries = parse_log(runs)
        board = Board(gql, entries, cfg, now=now, dry=dry)
        if issue := next(
            (i for i in board.issues("in_progress") if i["identifier"] == ident), None
        ):
            sid = board.current_sid(issue)
            if not board.settle(issue, sid, wait=False):
                return 0 if board.replays.get(ident) else stop
            board.plan(("resume", issue, sid))
            spec = Spec.from_issue(
                issue, mode="resume", sid=sid, task=None, client=client_of(entries, sid)
            )
        elif board.is_blocked(ident) or not (taken := board.take(ident)):
            return stop
        else:
            issue, task, _, client = taken
            spec = Spec.from_issue(
                issue, mode="new", sid=str(uuid.uuid4()), task=task, client=client
            )
        if dry:
            return 0
        return start(begin(cfg, spec))


def brake(role, cfg):
    usage = read_usage(role, cfg)
    print(summary(usage))
    return 1 if usage is None or exhausted(usage, BRAKE_5H) else 0


def options(argv):
    opts = {"brake": None, "dry": False, "now": False, "tui": False}
    opts.update(dict.fromkeys(VALUED.values()))
    if "--brake" in argv:
        ok = len(argv) == 2 and argv[0] == "--brake" and not argv[1].startswith("-")
        return {**opts, "brake": argv[1]} if ok else None
    i = 0
    while i < len(argv):
        a, value = argv[i], argv[i + 1] if i + 1 < len(argv) else None
        if a in FLAGS:
            opts[FLAGS[a]] = True
        elif a in VALUED and opts[VALUED[a]] is None and value not in (None, *VALUED, *FLAGS):
            opts[VALUED[a]] = value
            i += 1
        else:
            return None
        i += 1
    if opts["issue"] is not None and opts["now"]:
        return None
    if not opts["tui"] and (opts["display"] is not None or opts["events"] is not None):
        return None
    return opts


def attended_runs(opts, cfg):
    """A --tui router's Attended, its display and events checked up front; None without --tui.
    ConfigError: no events file, or the Delegate refuses them.
    """
    if not opts["tui"]:
        return None
    events = opts["events"] or os.environ.get(EVENTS_ENV) or None
    if events is None:
        raise ConfigError(f"--tui needs --events FILE or ${EVENTS_ENV}")
    attended = Attended(opts["display"], os.path.abspath(events))
    try:
        LinearRun.check_tui(cfg=cfg, display=attended.display, events=attended.events)
    except delegate.ConfigError as error:
        raise ConfigError(str(error)) from None
    return attended


def main(
    argv,
    *,
    now=datetime.now,
    config_path=None,
    hour=None,
    gql=None,
    run=None,
    keychain=None,
    start=None,
    delegate_cls=None,
):
    gql = gql or linear_gql
    run = run or config.shell
    keychain = keychain or has_key
    if argv in (["--help"], ["-h"]):
        print(__doc__)
        return 0
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
    except ConfigError as error:
        config.report_error("router", error, dry=opts["dry"])
        return 1
    event_log.use_logs(cfg.logs_dir)
    if delegate_cls is not None:
        cfg = replace(cfg, delegates={**cfg.delegates, cfg.delegate_kind: delegate_cls})
    if opts["brake"] is not None:
        role = opts["brake"]
        if role not in cfg.roles:
            print(f"router.py: no role {role}", file=sys.stderr)
            return 2
        return brake(role, cfg)
    try:
        attended = attended_runs(opts, cfg)
    except ConfigError as error:
        print(f"router.py: {error}", file=sys.stderr)
        return 2
    gql = functools.partial(gql, service=cfg.harness_key)
    if start is None:
        start = functools.partial(
            outer, cfg=cfg, gql=gql, run=run, keychain=keychain, now=now, attended=attended
        )
    try:
        if opts["issue"]:
            return run_issue(opts, cfg, gql, start, clock=now)
        return tick(
            opts,
            cfg,
            gql,
            hour=hour,
            start=start,
            clock=now,
        )
    except LinearError as e:
        return event_log.config_error("router", e, dry=opts["dry"])
    except Unavailable as e:
        return linear_error("router", e, dry=opts["dry"])


def start_run(start, args, say):
    try:
        result = start(args)
    except (Exception, SystemExit) as error:  # noqa: BLE001  # A recorded launch may fail.
        say(
            "launch-error",
            args.issue,
            error=one_line(error),
            traceback="".join(format_exception(error)),
        )
    else:
        say("launch", args.issue, exit=result)
    return 0
