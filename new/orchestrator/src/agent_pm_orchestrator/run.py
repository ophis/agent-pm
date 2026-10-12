#!/usr/bin/env python3
"""One agent run of an issue's role through delegate.run: its events write back to Linear (the session comment as the
harness account, progress and outcome as the role's), its usage limit to Linear and runs.jsonl. Exits 2 on bad argv,
1 on a config error, else 0."""

import argparse
import functools
import os
import re
import signal
import sys

from agent_pm import delegate
from agent_pm.driver import api
from agent_pm.utils.text import printable

from agent_pm_orchestrator import config, linear, runs, sessions, writeback
from agent_pm_orchestrator.linear import ISSUE_ID, log, one_line


def _stop(signum, frame):
    raise SystemExit(128 + signum)  # the Delegate then stops its session


def _close_tuis(a, role):
    try:
        found = api.sessions(re.escape(config.tui_prefix(role, a.issue)) + ".*")
    except api.DriverError:
        log("run", "tui-error", a.issue, msg="tmux sessions unknown")
        return
    for name in (row.name for row in found):
        if config.tui_issue(name) != (role, a.issue):
            continue
        try:
            api.close(name)
        except (api.ConfigError, api.DriverError) as e:
            log("run", "tui-error", a.issue, session=name, msg=one_line(e))
        else:
            log("run", "tui-closed", a.issue, session=name)


def inner(a, cfg, *, gql, run=delegate.run):
    os.environ["PATH"] = config.path()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _stop)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, api.SIGNALS)  # api.detach blocked them until now
    role = config.role_for(cfg.roles, a.assignee)
    if role is None:
        reason = f"{a.assignee!r} is not a role account"
    elif a.task is not None and a.task not in cfg.roles[role].tasks:
        reason = (
            f"task {a.task!r} is not one of {role}'s tasks ({', '.join(cfg.roles[role].tasks)})"
        )
    else:
        reason = None
    if reason:
        log("run", "config-error", a.issue, reason=reason)
        log("run", "end", a.issue, sid=a.sid, exit=1)
        return 1
    log("run", "launch", a.issue, mode=a.mode, sid=a.sid)
    _close_tuis(a, role)
    rd = cfg.run_dir(a.issue)
    harness = functools.partial(gql, service=cfg.harness_key, timeout=sessions.LIMIT)
    rec = sessions.base(sid=a.sid, workdir=rd, runtime=a.runtime, started_at=sessions.now())
    ctx = writeback.context(
        cfg,
        role,
        gql,
        ident=a.issue,
        issue_id=a.uuid,
        sid=a.sid,
        resume=a.mode == "resume",
        project=a.project,
        target=config.repo_slug(a.target) if a.target else None,
    )
    name = config.tui_name(role, a.issue, a.sid) if a.tui else None

    def post(rc=None, *, outcome=None, limit=False):
        if error := sessions.post(a.issue, rec, rc, gql=harness, outcome=outcome, limit=limit):
            log("run", "registry-error", a.issue, sid=a.sid, error=error)

    def on_event(event):
        if isinstance(event, delegate.Begun):
            post()  # after Begun: run.jsonl names the session's cwd, which the resume command needs
            if name is not None:
                try:
                    if command := api.show(name, a.split, a.split_from, a.opener):
                        print(printable(command), file=sys.stderr, flush=True)
                except (api.ConfigError, api.DriverError) as e:
                    log("run", "tui-error", a.issue, session=name, msg=one_line(e))
        elif isinstance(event, delegate.Progress):
            writeback.progress(ctx, event)
        elif isinstance(event, delegate.Finished):
            writeback.finish(ctx, event.result.outcome)

    rc, result = 1, None
    try:
        os.makedirs(rd, exist_ok=True)
        result = run(
            cfg.roles[role].run,
            a.task,
            a.input,
            rd,
            runtime=a.runtime,
            mode="tui" if a.tui else "headless",
            sid=a.sid,
            resume=a.sid if a.mode == "resume" else None,
            name=name,
            events=a.events,
            out=os.path.join(rd, "out.md"),
            cwd=rd,
            on_event=on_event,
        )
        rc = result.returncode
    except (Exception, SystemExit) as e:
        if isinstance(e, SystemExit) and isinstance(e.code, int):
            rc = e.code
        else:
            log("run", "run-error", a.issue, error=one_line(e))
    finally:
        log("run", "end", a.issue, sid=a.sid, exit=rc)
        if result and result.limit:
            at = result.limit.resets_at
            writeback.limit(ctx, at)
            try:
                runs.note_limit(
                    cfg.runs_log,
                    issue=a.issue,
                    sid=a.sid,
                    runtime=a.runtime,
                    resets_at=writeback.local_time(at),
                )
            except Exception as e:
                log("run", "run-error", a.issue, error=one_line(e))
        elif not (result and result.outcome):
            log("run", "no-outcome", a.issue, error=(result.error if result else "") or "no result")
        post(rc, outcome=result.outcome if result else None, limit=bool(result and result.limit))
        if error := sessions.repair(a.issue, a.uuid, rec, gql=harness):
            log("run", "registry-error", a.issue, sid=a.sid, error=error)
    return 0


def main(argv, *, gql=linear.linear_gql, run=delegate.run):
    ap = argparse.ArgumentParser(prog="run.py")
    for f in ("--uuid", "--issue", "--project", "--assignee", "--sid", "--runtime", "--input"):
        ap.add_argument(f, required=True)
    ap.add_argument("--mode", choices=("new", "resume"), required=True)
    ap.add_argument("--task")
    ap.add_argument("--target")
    ap.add_argument("--tui", action="store_true")
    for f in ("--split", "--split-from", "--opener", "--events"):
        ap.add_argument(f)
    a = ap.parse_args(argv)
    if not re.fullmatch(ISSUE_ID, a.issue) or not config.UUID_RE.fullmatch(a.sid):
        return _usage(f"bad issue or session id: {a.issue} {a.sid}")
    if not a.tui and any(v is not None for v in (a.split, a.split_from, a.opener, a.events)):
        return _usage("--split, --split-from, --opener and --events need --tui")
    if (
        not config.UUID_RE.fullmatch(a.uuid)
        or a.target is not None
        and not config.repo_slug(a.target)
    ):
        return _usage(f"bad issue uuid or target: {a.uuid} {a.target}")
    if not a.runtime or not a.runtime.isprintable():
        return _usage(f"bad runtime: {a.runtime!r}")
    try:
        cfg = config.load()
    except config.ConfigError as e:
        if e.logs_dir:
            linear.use_logs(e.logs_dir)
        else:
            print(e.msg, file=sys.stderr)  # no log dir yet
        log("run", "config-error", a.issue, reason=e.msg)
        log("run", "end", a.issue, sid=a.sid, exit=1)
        return 1
    linear.use_logs(cfg.logs_dir)
    return inner(a, cfg, gql=gql, run=run)


def _usage(msg):
    print(f"run.py: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
