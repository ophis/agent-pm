#!/usr/bin/env python3
"""Runner: one core agent run, per orchestrator/config.toml, of an issue claimed by the router or the attended entry (or
one the router resumes).

run.py --issue ID --project PROJECT_ID --assignee EMAIL --sid SID --task TASK --mode new|resume
       [--runner headless|tui] [--split right|below] [--beside SESSION] [--events FILE]
  Outer: checks the agent run can start, bounces an engineering issue whose repo check fails, writes <work_dir>/work/<ID>/input.md,
  then starts the inner in tmux agent-pm-<role>-<ID>; any failure starts nothing. Exits 0 started or bounced, 1 config,
  input or tmux failure, 2 bad arguments, not a role account or config error, 3 transient; a config error or transient
  failure is also logged to the task's project log. --runner tui (--split, --beside and --events need it) checks first
  where the TUI pane goes and the caller's opener (attended.layout), the events file (created 0600) and the session
  name (attended.prefix), exit 2 when one fails; the inner gets the opener and only the options given, and the
  sessions' attach commands go to stderr.
run.py --issue ID --tui [--split right|below] [--beside SESSION] [--events FILE]
  Attended entry, by hand: claims the issue (router.Board.take, without the tick's hours, max_runs or usage gate), logs
  its start line, then runs the outer with --runner tui. Exits 2 bad arguments, no place for the TUI pane or a bad
  events file, 1 config error, a live agent run of the issue or nothing claimed, else the outer's code.
run.py --inner --uuid ISSUE_UUID [--target OWNER/NAME] [--opener OPENER] <the same arguments>
  Inner, in that tmux session: attended.close (the issue's TUI sessions), the session comments, core's agent run with
  the orchestrator's sinks (tui: session <role>-<ID>-<sid[:8]>, its pane placed by --split/--beside, else stacked with
  the opener's), the end lines in the project log and runs.log, then write-back as the role account.
Needs Python 3.11+.
"""
import argparse
import functools
import os
import re
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inputs  # noqa: E402
import issues  # noqa: E402
import config  # noqa: E402
import attended  # noqa: E402
import router  # noqa: E402
import sessions  # noqa: E402
import target  # noqa: E402
import writeback  # noqa: E402
from config import (PATH, PROJECTS, ROOT, RUNS_LOG, TASKS, UUID_RE, load_config, project_log,  # noqa: E402
                    repo_slug, role_for, run_dir, runnable, session, sh_run, transcript)
from linear import ISSUE_ID, append, linear_gql, one_line  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402
import tui_claude  # noqa: E402

RUN = os.path.abspath(__file__)
SHARED = ("issue", "project", "assignee", "sid", "task", "mode")


def has_key(service):
    """True when the Keychain has an item for service; the secret is never read (no -w)."""
    return subprocess.run(["security", "find-generic-password", "-s", service],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def fail(plog, issue, kind, reason, rc):
    """An agent run that does not start: one `<kind>` line in the project log and on stderr; returns the exit code."""
    print(append(plog, f"{kind} {issue}: {reason}"), file=sys.stderr)
    return rc


def _append(path, line):
    """Best effort: the inner's lines must not stop it (its pane may be gone)."""
    try:
        append(path, line)
    except OSError:
        pass


def _humans(cfg):
    """The human_members emails, lowercased, in config order."""
    return tuple(e.lower() for e in cfg.get("human_members") or [])


def _context(a, cfg, role, gql, plog, issue_id, repo):
    """The agent run's write-back Context, acting as the role account."""
    return writeback.Context(
        ident=a.issue, issue_id=issue_id, task=a.task, sid=a.sid, resume=a.mode == "resume", project=a.project,
        workdir=run_dir(a.issue), plog=plog, gql=functools.partial(gql, service=role.key), humans=_humans(cfg),
        states=cfg["states"], repos=cfg["project_repos"], team=cfg["team"], target=repo)


class Setup(Exception):
    """An agent run that cannot start: message, exit code (1 config, 2 role or task) and the project log known so far."""

    def __init__(self, msg, rc, plog=None):
        super().__init__(msg)
        self.msg, self.rc, self.plog = msg, rc, plog


def load(root):
    """(cfg, roles) of root's orchestrator/config.toml, else Setup."""
    try:
        cfg = load_config(os.path.join(root, "orchestrator", "config.toml"))
        return cfg, runnable(cfg, root)
    except SystemExit as e:
        raise Setup(str(e.code), 1)


def setup(a, root):
    """(cfg, roles, name, role, plog) for the agent run's assignee and task, else Setup."""
    cfg, roles = load(root)
    name = role_for(roles, a.assignee)
    if name is None:
        raise Setup(f"{a.assignee!r} is not a role account", 2)
    role, logs = roles[name], config.LOGS_DIR
    if a.task not in role.tasks:
        raise Setup(f"task {a.task!r} is not one of {name}'s tasks ({', '.join(role.tasks)})", 2,
                    project_log(role.default, logs))
    return cfg, roles, name, role, project_log(a.task, logs)


def outer(a, *, sh, gql, run, projects, keychain, root):
    """In the router tick: check, bounce or prepare the agent run, then start the inner in tmux."""
    os.environ["PATH"] = PATH
    layout = events = None
    if a.runner == "tui":
        try:
            layout = attended.layout(a.split, a.beside)
            events = tui_claude.events_file(a.events) if a.events is not None else None
        except (attended.Bad, tui_claude.TuiError) as e:
            print(f"run.py: {e}", file=sys.stderr)
            return 2
    try:
        cfg, roles, name, role, plog = setup(a, root)
    except Setup as e:
        if e.plog:
            return fail(e.plog, a.issue, "config-error", e.msg, e.rc)
        print(f"run.py: {e.msg}", file=sys.stderr)
        return e.rc
    if layout:
        try:
            tui = drive.tui_session(name, a.task, a.sid, prefix=attended.prefix(name, a.issue))
        except ValueError as e:
            return fail(plog, a.issue, "config-error", str(e), 2)
    if a.mode == "resume":
        path = transcript(a.issue, a.sid, projects)
        if path is None or not os.path.exists(path):
            return fail(plog, a.issue, "transient", f"no transcript to resume at {path}", 3)
    if not keychain(role.key):
        return fail(plog, a.issue, "config-error", f"no Keychain item for role key {role.key}", 2)
    try:
        issue = issues.read_issue(gql, a.issue)
    except (Exception, SystemExit) as e:  # SystemExit: linear_gql's API error
        return fail(plog, a.issue, "transient", f"Linear: {one_line(e)}", 3)
    kind, repos, repo = TASKS[a.task].kind, cfg["project_repos"], None
    if kind == "build":
        try:
            repo = target.check(issue, repos, run=run, work=config.RUNS_DIR)
        except Exception as e:  # a gh/git timeout or OS error
            return fail(plog, a.issue, "transient", f"repo check: {one_line(e)}", 3)
        if isinstance(repo, target.Transient) or isinstance(repo, target.Invalid) and a.mode == "resume":
            return fail(plog, a.issue, "transient", repo.reason, 3)  # never bounce a mid-build agent run
        if isinstance(repo, target.Invalid):
            try:
                writeback.bounce(_context(a, cfg, role, gql, plog, issue.id, None), issue, repo.reason)
            except (Exception, SystemExit) as e:
                return fail(plog, a.issue, "transient", f"bounce: {one_line(e)}", 3)
            _append(plog, f"bounce {a.issue}: {repo.reason}")
            return 0
    else:
        repo = target.research_repo(issue, repos)
    if repo is not None:
        repo = target.with_clone(repo, a.issue, cfg["local_clones"], work=config.RUNS_DIR, run=run)
    rd = run_dir(a.issue)
    docs = config.docs(roles, root)
    try:
        sources = inputs.gather(issue, a.task, docs, run=run)
    except Exception as e:  # gh's RuntimeError; a decode, timeout or OS error too
        return fail(plog, a.issue, "transient", f"docs: {e}", 3)
    try:
        os.makedirs(rd, exist_ok=True)
        drive.save(os.path.join(rd, "input.md"),
                   inputs.render(issue, a.task, sources, humans=_humans(cfg), target=repo, docs=docs))
    except Exception as e:
        print(f"run.py: input.md: {one_line(e)}", file=sys.stderr)
        return 1
    attended_argv, iterm = [], []
    if layout:
        iterm = ["-e", f"ITERM_SESSION_ID={os.environ.get('ITERM_SESSION_ID', '')}"]  # tmux's own may be stale
        given = (("split", layout.split), ("beside", layout.beside), ("opener", layout.opener), ("events", events))
        attended_argv = ["--runner=tui", *(f"--{k}={v}" for k, v in given if v is not None)]
        print(f"run.py: driver: tmux attach -t '={session(name, a.issue)}'", file=sys.stderr)
        print(f"run.py: tui: tmux attach -t '={tui}'", file=sys.stderr)
    try:
        sh(["tmux", "new-session", "-d", *iterm, "-s", session(name, a.issue), "-c", rd, sys.executable, RUN,
            "--inner", "--uuid", issue.id, *(["--target", f"{repo.owner}/{repo.name}"] if kind == "build" else []),
            *(f"--{k}={getattr(a, k)}" for k in SHARED), *attended_argv], check=True)
    except subprocess.CalledProcessError as e:
        print(f"run.py: tmux: {one_line(e)}", file=sys.stderr)
        return 1
    return 0


def attended_run(a, *, sh, gql, run, runs, projects, keychain, root):
    """run.py --issue ID --tui: claim the issue, log its start line, then the outer with the tui runner."""
    os.environ["PATH"] = PATH
    if not re.fullmatch(ISSUE_ID, a.issue):
        print(f"run.py: bad issue id: {a.issue}", file=sys.stderr)
        return 2
    try:
        cfg, roles = load(root)
    except Setup as e:
        print(f"run.py: {e.msg}", file=sys.stderr)
        return e.rc
    try:
        attended.layout(a.split, a.beside)
        if a.events is not None:
            tui_claude.events_file(a.events)
    except (attended.Bad, tui_claude.TuiError) as e:
        print(f"run.py: {e}", file=sys.stderr)
        return 2
    live = router.live_sessions(roles, sh)
    if role := next((r for r, ids in live.items() if a.issue in ids), None):
        print(f"run.py: {a.issue} has a live agent run: tmux attach -t '={session(role, a.issue)}'", file=sys.stderr)
        return 1
    board = router.Board(gql, router.parse_log(runs), projects, datetime.now(timezone.utc), dry=False, cfg=cfg, root=root)
    taken = board.take(a.issue)
    if taken is None:
        return 1
    issue, task = taken
    sid = str(uuid.uuid4())
    append(runs, router.start_line(a.issue, sid, task))
    hosted = argparse.Namespace(issue=a.issue, project=issue["project"]["id"], assignee=issue["assignee"]["email"], sid=sid,
                                task=task, mode="new", runner="tui", split=a.split, beside=a.beside, events=a.events)
    return outer(hosted, sh=sh, gql=gql, run=run, projects=projects, keychain=keychain, root=root)


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
        cfg, roles, name, role, plog = setup(a, root)
    except (Exception, SystemExit) as e:
        plog = e.plog if isinstance(e, Setup) else None
        print(f"run.py: {e.msg if isinstance(e, Setup) else e.code if isinstance(e, SystemExit) else one_line(e)}",
              file=sys.stderr)
        line = f"end {a.issue} session={a.sid} exit=1"
        for path in (runs, plog) if plog else (runs,):
            _append(path, line)
        return 1
    rd, harness = run_dir(a.issue), functools.partial(gql, timeout=sessions.LIMIT)
    _append(plog, f"launch {a.issue} mode={a.mode} session={a.sid}")
    for c in attended.close(a.issue):
        where = a.issue if c.name is None else f"{a.issue} {c.name}"
        _append(plog, f"tui-{c.status} {where}: {c.msg}" if c.msg else f"tui-{c.status} {where}")
    rec = sessions.base(sid=a.sid, workdir=rd, started_at=sessions.now())

    def begun():  # run.json now names the session's cwd, which the comment's resume command needs
        if reg := sessions.post(a.issue, rec, gql=harness):
            _append(plog, reg)
    ctx = _context(a, cfg, role, gql, plog, a.uuid, repo_slug(a.target) if a.target else None)
    rc, result = 1, None
    try:
        core = os.path.join(root, "core")
        client = clients.get("claude", core)
        params = compose.RunParams(input=os.path.join(rd, "input.md"), out=os.path.join(rd, "deliverable.md"),
                                   workdir=rd, sid=a.sid, resume=a.mode == "resume")
        launch, run = drive.plan(core, client, name, a.task, params=params, layers=config.layers(root), cwd=rd)
        with open(plog, "a", encoding="utf-8", errors="replace") as err:  # claude's stderr outlives the pane, as live's `2>&1 | tee -a <plog>` did
            sinks = [drive.terminal(sys.stderr), drive.terminal(err), writeback.sink(ctx)]
            result = drive.start(launch, run, params, client=client, runner=a.runner, layout=layout,
                                 prefix=attended.prefix(name, a.issue) if layout else None, events=a.events,
                                 sinks=sinks, begun=begun, popen=functools.partial(popen, stderr=err))
        rc = result.returncode
    except (Exception, SystemExit) as e:
        if isinstance(e, SystemExit) and isinstance(e.code, int):
            rc = e.code  # the signal handler's
        else:
            _append(plog, f"run-error {a.issue}: {one_line(e)}")
    finally:
        line = f"end {a.issue} session={a.sid} exit={rc}"
        _append(plog, line)
        _append(runs, line)
        if result and result.outcome:
            writeback.finish(ctx, result.outcome)
        else:
            _append(plog, f"no-outcome {a.issue}: {(result.error if result else '') or 'no result'}")
        if reg := sessions.post(a.issue, rec, rc, gql=harness):
            _append(plog, reg)
    return 0


def main(argv, *, sh=subprocess.run, gql=linear_gql, run=sh_run, popen=subprocess.Popen,
         runs=RUNS_LOG, projects=PROJECTS, keychain=has_key, root=ROOT):
    """--tui → the attended entry, --inner → inner, else outer."""
    if "--tui" in argv:
        ap = argparse.ArgumentParser(prog="run.py", allow_abbrev=False)
        ap.add_argument("--issue", required=True)
        ap.add_argument("--tui", action="store_true")
        ap.add_argument("--split")
        ap.add_argument("--beside")
        ap.add_argument("--events")
        return attended_run(ap.parse_args(argv), sh=sh, gql=gql, run=run, runs=runs, projects=projects, keychain=keychain,
                            root=root)
    ap = argparse.ArgumentParser(prog="run.py")
    for f in ("--issue", "--project", "--assignee", "--sid", "--task"):
        ap.add_argument(f, required=True)
    ap.add_argument("--mode", choices=("new", "resume"), required=True)
    ap.add_argument("--inner", action="store_true")
    ap.add_argument("--uuid")
    ap.add_argument("--target")
    ap.add_argument("--runner", choices=("headless", "tui"), default="headless")
    ap.add_argument("--split")
    ap.add_argument("--beside")
    ap.add_argument("--opener")
    ap.add_argument("--events")
    a = ap.parse_args(argv)
    if not re.fullmatch(ISSUE_ID, a.issue) or not UUID_RE.fullmatch(a.sid):
        print(f"run.py: bad issue or session id: {a.issue} {a.sid}", file=sys.stderr)
        return 2
    if a.runner != "tui" and any(v is not None for v in (a.split, a.beside, a.opener, a.events)):
        print("run.py: --split, --beside, --opener and --events need --runner tui", file=sys.stderr)
        return 2
    if not a.inner:
        return outer(a, sh=sh, gql=gql, run=run, projects=projects, keychain=keychain, root=root)
    if not UUID_RE.fullmatch(a.uuid or "") or a.target is not None and not repo_slug(a.target):
        print(f"run.py: bad issue uuid or target: {a.uuid} {a.target}", file=sys.stderr)
        return 2
    layout = drive.Layout(a.split, a.beside, a.opener) if a.runner == "tui" else None
    try:
        drive.check_layout(a.runner, layout)  # syntax only: the outer checked where the pane goes
    except compose.ConfigError as e:
        print(f"run.py: {e}", file=sys.stderr)
        return 2
    return inner(a, layout=layout, gql=gql, popen=popen, runs=runs, root=root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
