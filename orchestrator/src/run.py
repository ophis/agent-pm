#!/usr/bin/env python3
"""Inner: one core agent run of an issue, per orchestrator/config.toml, in the driver's tmux session that router.py's
outer starts (CLAUDE.md › Architecture); nobody runs it by hand.

run.py --uuid ISSUE_UUID [--target OWNER/NAME] --issue ID --project PROJECT_ID --assignee EMAIL --sid SID [--task TASK]
       --mode new|resume [--runner headless|tui] [--split right|below] [--split-from SESSION] [--opener OPENER]
       [--events FILE] --input TEXT
  Exits 2 bad arguments, 1 a config, role or task failure, else 0. Past the argument checks an `end` event
  (orchestrator.jsonl, src run) always follows, with the agent run's own code (1 when it never started).
"""
import argparse
import functools
import os
import re
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import attended  # noqa: E402
import router  # noqa: E402
import sessions  # noqa: E402
import writeback  # noqa: E402
from config import PATH, ROOT, UUID_RE, repo_slug, run_dir  # noqa: E402
from linear import ISSUE_ID, linear_gql, log, one_line  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402


def inner(a, *, layout, gql, popen, root):
    """In tmux, cwd <work_dir>/work/<ID>: 1 when config, role or task fails, else 0; the agent run's own code goes to
    the `end` event. `layout` is the tui runner's, None for headless."""
    os.environ["PATH"] = PATH

    def stop(signum, frame):
        raise SystemExit(128 + signum)  # drive's `except BaseException` then kills claude
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, drive.SIGNALS)   # blocked by drive.detach until now
    try:
        cfg, roles, name, role = router.setup(a, root)
    except (Exception, SystemExit) as e:
        log("run", "config-error", a.issue, reason=e.msg if isinstance(e, router.Setup) else one_line(e))
        log("run", "end", a.issue, sid=a.sid, exit=1)
        return 1
    rd, harness = run_dir(a.issue), functools.partial(gql, timeout=sessions.LIMIT)
    log("run", "launch", a.issue, mode=a.mode, sid=a.sid)
    for c in attended.close(a.issue):
        log("run", f"tui-{c.status}", a.issue, session=c.name, msg=c.msg or None)
    rec = sessions.base(sid=a.sid, workdir=rd, started_at=sessions.now())

    def post(rc=None):  # as begun: run.jsonl now names the session's cwd, which the comment's resume command needs
        if error := sessions.post(a.issue, rec, rc, gql=harness):
            log("run", "registry-error", a.issue, sid=a.sid, error=error)
    ctx = router.context(a, cfg, name, role, gql, a.uuid, repo_slug(a.target) if a.target else None)
    rc, result = 1, None
    try:
        core = os.path.join(root, "core")
        client = clients.get("claude", core)
        params = compose.RunParams(input=a.input, out=os.path.join(rd, "out.md"),
                                   workdir=rd, sid=a.sid, resume=a.mode == "resume",
                                   prefix=attended.prefix(name, a.issue) if layout else None)
        launch, run = drive.plan(core, client, name, a.task, params=params, layers=config.layers(root), cwd=rd)
        result = drive.start(launch, run, params, client=client, runner=a.runner, layout=layout, events=a.events,
                             sinks=[drive.terminal(sys.stderr), writeback.sink(ctx)], begun=post, popen=popen)
        rc = result.returncode
    except (Exception, SystemExit) as e:
        if isinstance(e, SystemExit) and isinstance(e.code, int):
            rc = e.code  # the signal handler's
        else:
            log("run", "run-error", a.issue, error=one_line(e))
    finally:
        log("run", "end", a.issue, sid=a.sid, exit=rc)
        if result and result.outcome:
            writeback.finish(ctx, result.outcome)
        else:
            log("run", "no-outcome", a.issue, error=(result.error if result else "") or "no result")
        post(rc)
    return 0


def main(argv, *, gql=linear_gql, popen=subprocess.Popen, root=ROOT):
    ap = argparse.ArgumentParser(prog="run.py")
    for f in ("--uuid", "--issue", "--project", "--assignee", "--sid", "--input"):
        ap.add_argument(f, required=True)
    ap.add_argument("--mode", choices=("new", "resume"), required=True)
    ap.add_argument("--task")
    ap.add_argument("--target")
    ap.add_argument("--runner", choices=("headless", "tui"), default="headless")
    ap.add_argument("--split")
    ap.add_argument("--split-from")
    ap.add_argument("--opener")
    ap.add_argument("--events")
    a = ap.parse_args(argv)
    if not re.fullmatch(ISSUE_ID, a.issue) or not UUID_RE.fullmatch(a.sid):
        print(f"run.py: bad issue or session id: {a.issue} {a.sid}", file=sys.stderr)
        return 2
    if a.runner != "tui" and any(v is not None for v in (a.split, a.split_from, a.opener, a.events)):
        print("run.py: --split, --split-from, --opener and --events need --runner tui", file=sys.stderr)
        return 2
    if not UUID_RE.fullmatch(a.uuid) or a.target is not None and not repo_slug(a.target):
        print(f"run.py: bad issue uuid or target: {a.uuid} {a.target}", file=sys.stderr)
        return 2
    layout = drive.Layout(a.split, a.split_from, a.opener) if a.runner == "tui" else None
    try:
        drive.check_layout(a.runner, layout)  # syntax only: the outer checked where the pane goes
    except compose.ConfigError as e:
        print(f"run.py: {e}", file=sys.stderr)
        return 2
    return inner(a, layout=layout, gql=gql, popen=popen, root=root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
