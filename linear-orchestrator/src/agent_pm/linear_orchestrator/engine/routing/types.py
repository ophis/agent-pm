import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.runs import runtime_of
from agent_pm.linear_orchestrator.engine.types import (
    LinearError,
)
from agent_pm.linear_orchestrator.engine.usage.types import Gate
from agent_pm.linear_orchestrator.linear import linear, sessions
from agent_pm.linear_orchestrator.linear.linear import (
    humans,
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


CAP_COMMENT = "Tried 4 times without finishing; needs a look."


INTERRUPTED = "The previous agent run was interrupted. Moving this issue back to the Todo queue."


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


@dataclass(frozen=True, kw_only=True)
class BoardPolicy:
    now: datetime
    dry: bool
    gate: Gate | None = None


class Board:
    def __init__(self, gql, entries, cfg, policy: BoardPolicy, say=None):
        self.gql, self.entries, self.now, self.dry = gql, entries, policy.now, policy.dry
        self.cfg, self.gate = cfg, policy.gate
        self.say: Callable[..., None] = say or functools.partial(log, "router", dry=policy.dry)
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

    def last_move(self, issue, state, *, by_user=False):
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

    def recent_session(self, ident):
        latest = latest_sid(self.entries, ident)
        if latest is None:
            return False
        status = self.status(ident, latest)
        if status is None:
            self.unknown(ident, latest)
            return True
        return status.last_active is not None and status.last_active > (self.now - LIVE).timestamp()

    def settle(self, issue, sid, *, wait=True):
        ident = issue["identifier"]
        if wait and self.recent_session(ident):
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
