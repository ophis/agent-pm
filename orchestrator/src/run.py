#!/usr/bin/env python3
"""Inner: one core agent run of an issue, per orchestrator/config.toml, in the driver's tmux session that router.py's
outer starts (CLAUDE.md › Architecture); nobody runs it by hand.

run.py --uuid ISSUE_UUID [--target OWNER/NAME] --issue ID --project PROJECT_ID --assignee EMAIL --sid SID [--task TASK]
       --mode new|resume [--runner headless|tui] [--split right|below] [--split-from SESSION] [--opener OPENER]
       [--events FILE]
  Exits 2 bad arguments, 1 a config, role or task failure, else 0; the agent run's own code goes to the end lines.
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
from config import PATH, ROOT, RUNS_LOG, UUID_RE, repo_slug, run_dir  # noqa: E402
from linear import ISSUE_ID, linear_gql, one_line  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402


def inner(a, *, layout, gql, popen, runs, root):
    """In tmux, cwd <work_dir>/work/<ID>: 1 when config, role or task fails, else 0; the agent run's own code goes to the end lines.
    `layout` is the tui runner's, None for headless."""
    os.environ["PATH"] = PATH

    def stop(signum, frame):
        raise SystemExit(128 + signum)  # drive's `except BaseException` then kills claude
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)
    plog = None
    try:
        cfg, roles, name, role, plog = router.setup(a, root)
    except (Exception, SystemExit) as e:
        plog = e.plog if isinstance(e, router.Setup) else None
        print(f"run.py: {e.msg if isinstance(e, router.Setup) else e.code if isinstance(e, SystemExit) else one_line(e)}",
              file=sys.stderr)
        line = f"end {a.issue} session={a.sid} exit=1"
        for path in (runs, plog) if plog else (runs,):
            router.append_quiet(path, line)
        return 1
    rd, harness = run_dir(a.issue), functools.partial(gql, timeout=sessions.LIMIT)
    router.append_quiet(plog, f"launch {a.issue} mode={a.mode} session={a.sid}")
    for c in attended.close(a.issue):
        where = a.issue if c.name is None else f"{a.issue} {c.name}"
        router.append_quiet(plog, f"tui-{c.status} {where}: {c.msg}" if c.msg else f"tui-{c.status} {where}")
    rec = sessions.base(sid=a.sid, workdir=rd, started_at=sessions.now())

    def begun():  # run.jsonl now names the session's cwd, which the comment's resume command needs
        if reg := sessions.post(a.issue, rec, gql=harness):
            router.append_quiet(plog, reg)
    ctx = router.context(a, cfg, name, role, gql, plog, a.uuid, repo_slug(a.target) if a.target else None)
    rc, result = 1, None
    try:
        core = os.path.join(root, "core")
        client = clients.get("claude", core)
        params = compose.RunParams(input=os.path.join(rd, "input.md"), out=os.path.join(rd, "deliverable.md"),
                                   workdir=rd, sid=a.sid, resume=a.mode == "resume",
                                   prefix=attended.prefix(name, a.issue) if layout else None)
        launch, run = drive.plan(core, client, name, a.task, params=params, layers=config.layers(root), cwd=rd)
        with open(plog, "a", encoding="utf-8", errors="replace") as err:  # claude's stderr outlives the pane
            sinks = [drive.terminal(sys.stderr), drive.terminal(err), writeback.sink(ctx)]
            result = drive.start(launch, run, params, client=client, runner=a.runner, layout=layout, events=a.events,
                                 sinks=sinks, begun=begun, popen=popen)
        rc = result.returncode
    except (Exception, SystemExit) as e:
        if isinstance(e, SystemExit) and isinstance(e.code, int):
            rc = e.code  # the signal handler's
        else:
            router.append_quiet(plog, f"run-error {a.issue}: {one_line(e)}")
    finally:
        line = f"end {a.issue} session={a.sid} exit={rc}"
        router.append_quiet(plog, line)
        router.append_quiet(runs, line)
        if result and result.outcome:
            writeback.finish(ctx, result.outcome)
        else:
            router.append_quiet(plog, f"no-outcome {a.issue}: {(result.error if result else '') or 'no result'}")
        if reg := sessions.post(a.issue, rec, rc, gql=harness):
            router.append_quiet(plog, reg)
    return 0


def main(argv, *, gql=linear_gql, popen=subprocess.Popen, runs=RUNS_LOG, root=ROOT):
    ap = argparse.ArgumentParser(prog="run.py")
    for f in ("--uuid", "--issue", "--project", "--assignee", "--sid"):
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
    return inner(a, layout=layout, gql=gql, popen=popen, runs=runs, root=root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
