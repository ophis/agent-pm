"""One agent run of an issue's role through delegate.run: its events write back to Linear (the
session comment as the harness account, progress and outcome as the role's), its usage limit to
Linear and runs.jsonl. Exits 2 on bad argv, 1 on a config error, else 0.
"""

import functools
import os
import re
import signal
import sys
from dataclasses import replace
from traceback import format_exception

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import client, config, run_comments, runs, writeback
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.log import log
from agent_pm.linear_orchestrator.engine.types import (
    ConfigError,
    Spec,
    Stopped,
)
from agent_pm.session import SIGNALS, DriverError
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.utils import one_line, printable, repo_slug, timestamp


def _stop(signum, _frame):
    raise Stopped(128 + signum)  # the Delegate then stops its session


def _close_tuis(a, role, cfg):
    try:
        found = cfg.driver.sessions(re.escape(config.tui_prefix(role, a.issue)) + ".*")
    except DriverError:
        log("run", "tui-error", a.issue, msg="tmux sessions unknown")
        return
    names = (row.name for row in found if config.tui_issue(row.name) == (role, a.issue))
    for name, error in run_comments.close_tuis(cfg.driver, names):
        if error:
            log("run", "tui-error", a.issue, session=name, msg=error)
        else:
            log("run", "tui-closed", a.issue, session=name)


def _run_role(a, cfg):
    role = config.role_for(cfg.roles, a.assignee)
    tasks = cfg.roles[role].tasks if role is not None else ()
    reason = a.role_error(role, tasks)
    if reason:
        log("run", "config-error", a.issue, reason=reason)
        log("run", "end", a.issue, sid=a.sid, exit=1)
        return None
    return role


def _report_outcome(a, cfg, ctx, result):
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
        except OSError as e:
            log("run", "run-error", a.issue, error=one_line(e))
    elif not (result and result.outcome):
        log("run", "no-outcome", a.issue, error=(result.error if result else "") or "no result")


def inner(a, cfg, *, gql, run=delegate.run, now=timestamp):
    os.environ["PATH"] = config.path()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _stop)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)  # session.detach blocked them until now
    role = _run_role(a, cfg)
    if role is None:
        return 1
    log("run", "launch", a.issue, mode=a.mode, sid=a.sid)
    _close_tuis(a, role, cfg)
    rd = cfg.run_dir(a.issue)
    harness = functools.partial(gql, service=cfg.harness_key, timeout=run_comments.LIMIT)
    rec = run_comments.base(
        sid=a.sid, workdir=rd, runtime=a.runtime, started_at=now(), driver=cfg.driver
    )
    ctx = writeback.for_run(cfg, role, gql, a)
    name = config.tui_name(role, a.issue, a.sid) if a.tui else None

    def post(rc=None, *, outcome=None, limit=False):
        if error := run_comments.post(
            a.issue,
            rec,
            gql=harness,
            rc=rc,
            ended_at=now(),
            outcome=outcome,
            limit=limit,
        ):
            log("run", "registry-error", a.issue, sid=a.sid, error=error)

    def on_event(event):
        if isinstance(event, delegate.Begun):
            post()  # after Begun: run.jsonl names the session's cwd, which the resume command needs
            if name is not None:
                try:
                    if command := cfg.driver.show(name, a.split, a.split_from, a.opener):
                        print(printable(command), file=sys.stderr, flush=True)
                except (SessionConfigError, DriverError) as e:
                    log("run", "tui-error", a.issue, session=name, msg=one_line(e))
        elif isinstance(event, delegate.Progress):
            writeback.progress(ctx, event)
        elif isinstance(event, delegate.Finished) and event.result.outcome is not None:
            writeback.finish(ctx, event.result.outcome)

    rc, result = 1, None
    try:
        os.makedirs(rd, exist_ok=True)
        spec = delegate.Spec(
            a.input,
            rd,
            task=a.task,
            mode="tui" if a.tui else "headless",
            runtime=a.runtime,
            sid=a.sid,
            resume=a.mode == "resume",
            name=name,
            events=a.events,
            out=os.path.join(rd, "out.md"),
            cwd=rd,
            protect=cfg.protect,
        )
        result = run(
            cfg.roles[role].run,
            spec,
            driver=cfg.driver,
            on_event=on_event,
        )
        rc = result.returncode
    except Stopped as e:
        rc = e.code
    except (Exception, SystemExit) as e:  # noqa: BLE001  # Finish reporting a failed run.
        if isinstance(e, SystemExit) and isinstance(e.code, int):
            rc = e.code
        log(
            "run",
            "run-error",
            a.issue,
            error=one_line(e),
            traceback="".join(format_exception(e)),
        )
    finally:
        log("run", "end", a.issue, sid=a.sid, exit=rc)
        _report_outcome(a, cfg, ctx, result)
        post(rc, outcome=result.outcome if result else None, limit=bool(result and result.limit))
        if error := run_comments.repair(a.issue, a.uuid, rec, gql=harness):
            log("run", "registry-error", a.issue, sid=a.sid, error=error)
    return 0


def main(argv, *, gql=client.linear_gql, run=delegate.run, driver=None, now=timestamp):
    a = Spec.from_argv(argv)
    if reason := a.identity_error():
        return _usage(reason)
    if not a.tui and any(v is not None for v in (a.split, a.split_from, a.opener, a.events)):
        return _usage("--split, --split-from, --opener and --events need --tui")
    if not config.UUID_RE.fullmatch(a.uuid) or a.target is not None and not repo_slug(a.target):
        return _usage(f"bad issue uuid or target: {a.uuid} {a.target}")
    if not a.runtime or not a.runtime.isprintable():
        return _usage(f"bad runtime: {a.runtime!r}")
    try:
        cfg = config.load()
    except ConfigError as e:
        if e.logs_dir:
            event_log.use_logs(e.logs_dir)
        else:
            print(e.msg, file=sys.stderr)  # no log dir yet
        log("run", "config-error", a.issue, reason=e.msg)
        log("run", "end", a.issue, sid=a.sid, exit=1)
        return 1
    event_log.use_logs(cfg.logs_dir)
    cfg = replace(cfg, driver=driver) if driver is not None else cfg
    return inner(a, cfg, gql=gql, run=run, now=now)


def _usage(msg):
    print(f"run.py: {msg}", file=sys.stderr)
    return 2
