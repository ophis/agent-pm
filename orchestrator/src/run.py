#!/usr/bin/env python3
"""Runner: one core run for an issue the router already claimed (or resumes it), per orchestrator/config.toml.

run.py --issue ID --project PROJECT_ID --assignee EMAIL --sid SID --task TASK --mode new|resume
  Outer, in the router tick: checks the run can start, bounces an engineering issue whose repo check fails, writes
  work/<ID>/input.md, then starts the inner in tmux agent-pm-<role>; any failure starts nothing. Exits 0 started or
  bounced, 1 config, input or tmux failure, 2 bad arguments, not a role account or config error, 3 transient; a config
  error or transient failure is also logged to the task's project log.
run.py --inner --uuid ISSUE_UUID [--target OWNER/NAME] <the same arguments>
  Inner, in that tmux session: the session comments, core's run with the orchestrator's sinks, the end lines in the
  project log and runs.log, then write-back as the role account.
Needs Python 3.11+.
"""
import argparse
import functools
import os
import re
import signal
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inputs  # noqa: E402
import issues  # noqa: E402
import config  # noqa: E402
import sessions  # noqa: E402
import target  # noqa: E402
import writeback  # noqa: E402
from config import (PATH, PROJECTS, ROOT, RUNS_LOG, TASKS, UUID_RE, load_config, project_log,  # noqa: E402
                    repo_slug, role_for, run_dir, runnable, session, sh_run, transcript)
from linear import atomic_write, linear_gql  # noqa: E402
from sessions import one_line  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402

RUN = os.path.abspath(__file__)
SHARED = ("issue", "project", "assignee", "sid", "task", "mode")


def has_key(service):
    """True when the Keychain has an item for service; the secret is never read (no -w)."""
    return subprocess.run(["security", "find-generic-password", "-s", service],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def fail(plog, issue, kind, reason, rc):
    """A run that does not start: one `<kind>` line in the project log and on stderr; returns the exit code."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {kind} {issue}: {reason}"
    with open(plog, "a") as f:
        f.write(line + "\n")
    print(line, file=sys.stderr)
    return rc


def _stamp():
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


def _append(path, line):
    """Best effort: the inner's lines must not stop it (its pane may be gone)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_file(path) -> drive.Sink:
    """The terminal sink's lines, appended to path (the project log); swallows OSError."""
    def sink(event):
        if event.kind == "text":
            line = event.text
        elif event.kind == "progress":
            line = f"Progress ({event.name}): {event.text}"
        else:
            return
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(drive.printable(line) + "\n")
        except OSError:
            pass
    return sink


def _humans(cfg):
    """The human_members emails, lowercased, in config order."""
    return tuple(e.lower() for e in cfg.get("human_members") or [])


def _context(a, cfg, role, gql, plog, issue_id, repo):
    """The run's write-back Context, acting as the role account."""
    return writeback.Context(
        ident=a.issue, issue_id=issue_id, task=a.task, sid=a.sid, resume=a.mode == "resume", project=a.project,
        workdir=run_dir(a.issue), plog=plog, gql=functools.partial(gql, service=role.key), humans=_humans(cfg),
        states=cfg["states"], repos=cfg["project_repos"], team=cfg["team"], target=repo)


def outer(a, *, sh, gql, run, projects, keychain, root):
    """In the router tick: check, bounce or prepare the run, then start the inner in tmux."""
    os.environ["PATH"] = PATH
    try:
        cfg = load_config(os.path.join(root, "orchestrator", "config.toml"))
        roles = runnable(cfg, root)
    except SystemExit as e:
        print(f"run.py: {e.code}", file=sys.stderr)
        return 1
    name = role_for(roles, a.assignee)
    if name is None:
        print(f"run.py: {a.assignee!r} is not a role account", file=sys.stderr)
        return 2
    role, logs = roles[name], os.path.join(root, "logs")
    if a.task not in role.tasks:
        return fail(project_log(role.default, logs), a.issue, "config-error",
                    f"task {a.task!r} is not one of {name}'s tasks ({', '.join(role.tasks)})", 2)
    plog = project_log(a.task, logs)
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
            repo = target.check(issue, repos, run=run, work=config.WORK)
        except Exception as e:  # a gh/git timeout or OS error
            return fail(plog, a.issue, "transient", f"repo check: {one_line(e)}", 3)
        if isinstance(repo, target.Transient) or isinstance(repo, target.Invalid) and a.mode == "resume":
            return fail(plog, a.issue, "transient", repo.reason, 3)  # never bounce a mid-build run
        if isinstance(repo, target.Invalid):
            try:
                writeback.bounce(_context(a, cfg, role, gql, plog, issue.id, None), issue, repo.reason)
            except (Exception, SystemExit) as e:
                return fail(plog, a.issue, "transient", f"bounce: {one_line(e)}", 3)
            _append(plog, f"{_stamp()} bounce {a.issue}: {repo.reason}")
            return 0
    else:
        repo = target.research_repo(issue, repos)
    rd = run_dir(a.issue)
    docs = config.docs(roles, root)
    try:
        sources = inputs.gather(issue, a.task, docs, run=run)
    except Exception as e:  # gh's RuntimeError; a decode, timeout or OS error too
        return fail(plog, a.issue, "transient", f"docs: {e}", 3)
    try:
        os.makedirs(rd, exist_ok=True)
        atomic_write(os.path.join(rd, "input.md"),
                     inputs.render(issue, a.task, sources, humans=_humans(cfg), target=repo, docs=docs))
    except Exception as e:
        print(f"run.py: input.md: {one_line(e)}", file=sys.stderr)
        return 1
    try:
        sh(["tmux", "new-session", "-d", "-s", session(name), "-c", rd, sys.executable, RUN, "--inner", "--uuid", issue.id,
            *(["--target", f"{repo.owner}/{repo.name}"] if kind == "build" else []),
            *(f"--{k}={getattr(a, k)}" for k in SHARED)], check=True)
    except subprocess.CalledProcessError as e:
        print(f"run.py: tmux: {one_line(e)}", file=sys.stderr)
        return 1
    return 0


def inner(a, *, gql, popen, runs, root):
    """In tmux, cwd work/<ID>: 1 when config, role or task fails, else 0; the run's own code goes to the end lines."""
    os.environ["PATH"] = PATH

    def stop(signum, frame):
        raise SystemExit(128 + signum)  # drive's `except BaseException` then kills claude
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    plog = None
    try:
        cfg = load_config(os.path.join(root, "orchestrator", "config.toml"))
        roles = runnable(cfg, root)
        name = role_for(roles, a.assignee)
        if name is None:
            raise SystemExit(f"{a.assignee!r} is not a role account")
        role, logs = roles[name], os.path.join(root, "logs")
        if a.task not in role.tasks:
            plog = project_log(role.default, logs)
            raise SystemExit(f"task {a.task!r} is not one of {name}'s tasks ({', '.join(role.tasks)})")
        plog = project_log(a.task, logs)
    except (Exception, SystemExit) as e:
        print(f"run.py: {e.code if isinstance(e, SystemExit) else one_line(e)}", file=sys.stderr)
        line = f"{_stamp()} end {a.issue} session={a.sid} exit=1"
        for path in (runs, plog) if plog else (runs,):
            _append(path, line)
        return 1
    rd, harness = run_dir(a.issue), functools.partial(gql, timeout=sessions.LIMIT)
    _append(plog, f"{_stamp()} launch {a.issue} mode={a.mode} session={a.sid}")
    rec = sessions.base(sid=a.sid, cwd=rd, started_at=sessions.now())
    if reg := sessions.post(a.issue, rec, gql=harness):
        _append(plog, reg)
    ctx = _context(a, cfg, role, gql, plog, a.uuid, repo_slug(a.target) if a.target else None)
    rc, result = 1, None
    try:
        core = os.path.join(root, "core")
        client = clients.get("claude", core)
        params = compose.RunParams(input=os.path.join(rd, "input.md"), out=os.path.join(rd, "deliverable.md"),
                                   workdir=rd, sid=a.sid, resume=a.mode == "resume")
        launch, run = drive.plan(core, client, name, a.task, params=params, layers=config.layers(root))
        sinks = [drive.terminal(sys.stderr), drive.progress_file(os.path.join(rd, drive.PROGRESS), append=params.resume),
                 drive.outcome_file(os.path.join(rd, drive.OUTCOME)), log_file(plog), writeback.sink(ctx)]
        with open(plog, "a") as err:  # claude's stderr outlives the pane, as live's `2>&1 | tee -a <plog>` did
            result = drive.start(launch, run, params, client=client, sinks=sinks,
                                 popen=functools.partial(popen, stderr=err))
        rc = result.returncode
    except (Exception, SystemExit) as e:
        if isinstance(e, SystemExit) and isinstance(e.code, int):
            rc = e.code  # the signal handler's
        else:
            _append(plog, f"{_stamp()} run-error {a.issue}: {one_line(e)}")
    finally:
        line = f"{_stamp()} end {a.issue} session={a.sid} exit={rc}"
        _append(plog, line)
        _append(runs, line)
        if result and result.outcome:
            writeback.finish(ctx, result.outcome)
        else:
            _append(plog, f"{_stamp()} no-outcome {a.issue}: {(result.error if result else '') or 'no result'}")
        if reg := sessions.post(a.issue, rec, rc, gql=harness):
            _append(plog, reg)
    return 0


def main(argv, *, sh=subprocess.run, gql=linear_gql, run=sh_run, popen=subprocess.Popen,
         runs=RUNS_LOG, projects=PROJECTS, keychain=has_key, root=ROOT):
    """--inner → inner, else outer."""
    ap = argparse.ArgumentParser(prog="run.py")
    for f in ("--issue", "--project", "--assignee", "--sid", "--task"):
        ap.add_argument(f, required=True)
    ap.add_argument("--mode", choices=("new", "resume"), required=True)
    ap.add_argument("--inner", action="store_true")
    ap.add_argument("--uuid")
    ap.add_argument("--target")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", a.issue) or not re.fullmatch(sessions.SID, a.sid):
        print(f"run.py: bad issue or session id: {a.issue} {a.sid}", file=sys.stderr)
        return 2
    if not a.inner:
        return outer(a, sh=sh, gql=gql, run=run, projects=projects, keychain=keychain, root=root)
    if not UUID_RE.fullmatch(a.uuid or "") or a.target is not None and not repo_slug(a.target):
        print(f"run.py: bad issue uuid or target: {a.uuid} {a.target}", file=sys.stderr)
        return 2
    return inner(a, gql=gql, popen=popen, runs=runs, root=root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
