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
from dataclasses import replace
from datetime import UTC, datetime

from agent_pm.delegate import PYTHONPATH
from agent_pm.linear_orchestrator.engine import config, inputs, startup, target
from agent_pm.linear_orchestrator.engine.routing.types import Board, BoardPolicy, Held
from agent_pm.linear_orchestrator.engine.runs import note, parse_log, runtime_of, trim
from agent_pm.linear_orchestrator.engine.types import (
    SERVICE_ERRORS,
    Invalid,
    RouterServices,
    Transient,
    Unavailable,
)
from agent_pm.linear_orchestrator.engine.usage.types import Gate, exhausted, read_usage, summary
from agent_pm.linear_orchestrator.linear import issues, linear, sessions, writeback
from agent_pm.linear_orchestrator.linear.linear import (
    ISSUE_ID,
    linear_error,
    linear_gql,
    log,
    one_line,
)
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError

BRAKE_5H = 80
BUSY = "another router is running"
USAGE = (
    "usage: python3 -P -m agent_pm.linear_orchestrator.engine.router "
    "[--now] [--dry-run] [--issue ID] | --brake ROLE"
)
FLAGS = {"--dry-run": "dry", "--now": "now"}
RUN = ("-P", "-m", "agent_pm.linear_orchestrator.engine.run")
SESSIONS = "agent-pm-"
SHARED = ("issue", "project", "assignee", "sid", "task", "mode", "runtime")


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


def begin(selection, runs):
    mode, issue, sid, role, task, runtime = selection
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
            gate = Gate(runs, now, dry=dry, say=walk, driver=cfg.driver)
            board = Board(gql, parse_log(runs), cfg, BoardPolicy(now=now, dry=dry, gate=gate), walk)
            run = choose(board, gate, {i for ids in live.values() for i in ids}, full, counts)
            if run is None or dry:
                return 0
            a = begin(run, runs)
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
        board = Board(gql, entries, cfg, BoardPolicy(now=now, dry=dry))
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
        return 0 if dry else start(begin(run, runs))


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
    argv, *, now=None, config_path=config.CONFIG, hour=None, services: RouterServices | None = None
):
    services = services or RouterServices()
    gql = services.gql or linear_gql
    run = services.run or config.sh_run
    keychain = services.keychain or linear.has_key
    start, driver = services.start, services.driver
    opts = options(argv)
    if opts is None:
        print(USAGE, file=sys.stderr)
        return 2
    if opts["issue"] is not None and not re.fullmatch(ISSUE_ID, opts["issue"]):
        print(f"router.py: bad issue id: {opts['issue']}", file=sys.stderr)
        return 2
    os.environ["PATH"] = config.path()
    cfg = startup.configured("router", config_path, dry=opts["dry"])
    if cfg is None:
        return 1
    cfg = replace(cfg, driver=driver) if driver is not None else cfg
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
        return linear_error("router", e, dry=opts["dry"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
