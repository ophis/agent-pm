"""One agent run of an issue's role, its LinearRun's Delegate run in this process: its events write
back to Linear (the session comment as the harness account, progress and outcome as the role's), its
usage limit to Linear and runs.jsonl. Exits 2 on bad argv, 1 on a config error, else 0.
"""

import functools
import os
import signal
import sys
from traceback import format_exception

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import client, config, run_comments, runs, writeback
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.log import log
from agent_pm.linear_orchestrator.engine.types import (
    ConfigError,
    Spec,
    Stopped,
)
from agent_pm.session import SIGNALS
from agent_pm.utils import one_line, repo_slug, timestamp


def _stop(signum, _frame):
    raise Stopped(128 + signum)  # the Delegate then stops its session


def _run_role(a, cfg):
    role = config.role_for(cfg.roles, a.assignee)
    tasks = cfg.roles[role].tasks if role is not None else ()
    reason = a.role_error(role, tasks)
    if reason:
        log("run", "config-error", a.issue, reason=reason)
        log("run", "end", a.issue, sid=a.sid, exit=1)
        return None
    return role


def _kind(sid, cfg):
    entries = runs.parse_log(cfg.runs_log)
    if any(e.sid == sid for e in entries):
        return runs.delegate_of(entries, sid)
    return cfg.delegate_kind


def _display(a):
    return "/".join((a.opener or "", a.split or "", a.split_from or "")).rstrip("/") or None


def _report_outcome(a, cfg, ctx, result):
    if result and result.limit:
        at = result.limit.resets_at
        writeback.limit(ctx, at)
        try:
            runs.note_limit(
                cfg.runs_log,
                issue=a.issue,
                sid=a.sid,
                client=a.client,
                resets_at=writeback.local_time(at),
                delegate_kind=_kind(a.sid, cfg),
            )
        except (OSError, ValueError) as e:  # ValueError: an undecodable runs.jsonl
            log("run", "run-error", a.issue, error=one_line(e))
    elif not (result and result.outcome):
        log("run", "no-outcome", a.issue, error=(result.error if result else "") or "no result")


def inner(a, cfg, *, gql, delegate_cls=None, now=timestamp):
    os.environ["PATH"] = config.path()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _stop)
    # The router's detach keeps these blocked through exec and imports.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
    role = _run_role(a, cfg)
    if role is None:
        return 1
    log("run", "launch", a.issue, mode=a.mode, sid=a.sid)
    rd = cfg.run_dir(a.issue)
    harness = functools.partial(gql, service=cfg.harness_key, timeout=run_comments.LIMIT)
    view = None

    def info():
        if view is None:
            raise delegate.ConfigError(f"{a.issue}: its Delegate was not built")
        return view.session_info()

    rec = run_comments.base(sid=a.sid, workdir=rd, client=a.client, started_at=now(), info=info)
    ctx = writeback.for_run(cfg, role, gql, a)

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
        elif isinstance(event, delegate.Progress):
            writeback.progress(ctx, event)
        elif isinstance(event, delegate.Finished) and event.result.outcome is not None:
            writeback.finish(ctx, event.result.outcome)

    rc, result = 1, None
    try:
        os.makedirs(rd, exist_ok=True)
        view = LinearRun(
            a.issue,
            a.input,
            rd,
            cfg=cfg,
            role=role,
            sid=a.sid,
            task=a.task,
            client=a.client,
            mode="tui" if a.tui else "headless",
            resume=a.mode == "resume",
            events=a.events,
            display=_display(a),
            delegate_kind=_kind(a.sid, cfg),
            delegate_cls=delegate_cls,
        )
        result = view.delegate.run(on_event)
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


def main(argv, *, gql=client.linear_gql, delegate_cls=None, now=timestamp):
    a = Spec.from_argv(argv)
    if reason := a.identity_error():
        return _usage(reason)
    if not a.tui and any(v is not None for v in (a.split, a.split_from, a.opener, a.events)):
        return _usage("--split, --split-from, --opener and --events need --tui")
    if not config.UUID_RE.fullmatch(a.uuid) or a.target is not None and not repo_slug(a.target):
        return _usage(f"bad issue uuid or target: {a.uuid} {a.target}")
    if not a.client or not a.client.isprintable():
        return _usage(f"bad client: {a.client!r}")
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
    return inner(a, cfg, gql=gql, delegate_cls=delegate_cls, now=now)


def _usage(msg):
    print(f"run.py: {msg}", file=sys.stderr)
    return 2
