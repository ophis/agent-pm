#!/usr/bin/env python3
"""Launcher: starts one claude run for an issue the router already claimed (or resumes it), per pipeline.toml.

launch.py --issue ID --url URL --project PROJECT_ID --sid SID --mode new|resume [--k K]
Every run works in work/<ID>/. Exits 2 for an unknown or non-runnable project, 3 when the run cannot start yet
(transient: no transcript to resume, or the Engineering repo step failed transiently). Needs Python 3.11+.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eng  # noqa: E402
from pipeline import (PATH, PLACEHOLDERS, PROJECTS, ROOT, RUNS_LOG, SESSION, linear_gql, load_config,  # noqa: E402
                      project_log, run_dir, runnable, transcript)

PRINCIPLES = os.path.join(ROOT, "stages", "principles.md")
# Set inside the tmux command: a running tmux server would otherwise supply its own environment.
ENV = {"PATH": PATH, "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"}  # claude -p otherwise kills a workflow after 10 idle minutes



def prompt(a, instructions):
    if a.mode == "resume":
        return (f"Resumed run {a.k} for {a.issue} ({a.url}) after an interruption. Re-read {PRINCIPLES} and {instructions} "
                "first (they may have changed since this session started) and follow the stage's resume rule.")
    return f"Follow {PRINCIPLES} and {instructions} to handle {a.issue} ({a.url}). The runner has already claimed it."


def deny(path):
    """Edit deny rule (covers every file-editing tool) for an absolute path; a single leading "/" would be relative to the project root."""
    return "Edit(//" + path.lstrip("/") + ")"


def command(a, p, tail="", allowed=()):
    dirs = [os.path.expanduser(d) for d in p.get("add_dirs", [])]
    denied = [os.path.join(ROOT, "stages", "**"), os.path.join(ROOT, "templates", "**"),
              os.path.join(run_dir(a.issue), "worktrees", "*", ".git")]
    if p.get("repo_from_issue"):
        denied += [os.path.join(d, "**") for d in dirs]
    cmd = ["claude", "-p", prompt(a, os.path.join(ROOT, p["instructions"])) + tail,
           "--resume" if a.mode == "resume" else "--session-id", a.sid,
           "--model", p["model"], "--effort", p["effort"], "--permission-mode", "auto",
           "--setting-sources", "user", "--strict-mcp-config",
           "--add-dir", os.path.join(ROOT, "stages"), "--add-dir", os.path.join(ROOT, "templates")]
    for d in dirs:
        cmd += ["--add-dir", d]
    cmd += ["--disallowedTools", *map(deny, denied)]
    if allowed:
        cmd += ["--allowedTools", *allowed]
    return cmd


def script(a, cmd, env, plog, runs):
    q = shlex.quote
    exports = " ".join(f"{k}={q(v)}" for k, v in env.items())
    end = f'"$ts end {a.issue} session={a.sid} exit=$rc"'
    return (f"export {exports}; "
            f'echo "$(date "+%F %T") launch {a.issue} mode={a.mode} session={a.sid}" >> {q(plog)}; '
            f"{shlex.join(cmd)} < /dev/null 2>&1 | tee -a {q(plog)}; rc=${{PIPESTATUS[0]}}; "
            f'ts=$(date "+%F %T"); echo {end} >> {q(plog)}; echo {end} >> {q(runs)}')


def transient(plog, issue, reason):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} transient {issue}: {reason}"
    with open(plog, "a") as f:
        f.write(line + "\n")
    print(line, file=sys.stderr)
    return 3


def repo_step(a, p, gql, run):
    """(prompt tail, extra env, allowed rules) for a repo_from_issue project, or a Transient."""
    try:
        r = eng.resolve(a.issue, gql, run)
    except Exception as e:  # a resolve bug must not crash the launcher: Transient lets Recover retry the issue
        return eng.Transient(f"resolve: {type(e).__name__}: {e}")
    if isinstance(r, eng.Invalid) and a.mode == "resume":
        return eng.Transient(r.reason)  # the launcher never bounces a mid-build run
    if isinstance(r, eng.Transient):
        return r
    cli = f" eng.py: python3 {shlex.quote(os.path.join(ROOT, 'scripts', 'eng.py'))}."
    if isinstance(r, eng.Invalid):
        return f" Repo check failed: {r.reason}.{cli}", {}, []
    tail = (f" Repo check: OK {r.owner}/{r.name}, clone {r.clone}, default branch {r.default}, branch {r.branch}, "
            f"worktree {r.worktree}.{cli}")
    values = {k: getattr(r, k) for k in PLACEHOLDERS}
    return tail, {"AGENT_PM_ISSUE": a.issue}, [t.format(**values) for t in p.get("allowed_tools", [])]


def main(argv, sh=subprocess.run, config=None, runs=RUNS_LOG, logs=None, gql=None, run=eng.sh_run, projects=PROJECTS):
    ap = argparse.ArgumentParser(prog="launch.py")
    for f in ("--issue", "--url", "--project", "--sid"):
        ap.add_argument(f, required=True)
    ap.add_argument("--mode", choices=("new", "resume"), required=True)
    ap.add_argument("--k", default="1")
    a = ap.parse_args(argv)
    # Issue and SID are interpolated into the tmux shell command.
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", a.issue) or not re.fullmatch(r"[0-9a-f-]{36}", a.sid):
        print(f"launch.py: bad issue or session id: {a.issue} {a.sid}", file=sys.stderr)
        return 2
    os.environ["PATH"] = PATH
    cfg = load_config(config) if config else load_config()
    stages = runnable(cfg)
    if a.project not in stages:
        print(f"launch.py: {a.project!r} is not a runnable project in pipeline.toml", file=sys.stderr)
        return 2
    p = stages[a.project]
    name = os.path.splitext(os.path.basename(p["instructions"]))[0]  # log named after the stage
    plog = project_log(name, logs) if logs else project_log(name)
    if a.mode == "resume":
        path = transcript(a.issue, a.sid, projects)
        if path is None or not os.path.exists(path):
            return transient(plog, a.issue, f"no transcript to resume at {path}")
    humans = cfg.get("human_members") or []
    tail = f" Reviewer: {(humans or ['none'])[0]}. Humans: {', '.join(humans) or 'none'}. Project: {a.project}."
    env, allowed = {}, []
    if p.get("repo_from_issue"):
        step = repo_step(a, p, gql or linear_gql, run)
        if isinstance(step, eng.Transient):
            return transient(plog, a.issue, step.reason)
        extra, env, allowed = step
        tail += extra
    cwd = run_dir(a.issue)
    os.makedirs(cwd, exist_ok=True)
    cmd = command(a, p, tail, allowed)
    sh(["tmux", "new-session", "-d", "-s", SESSION, "-c", cwd, "bash", "-c", script(a, cmd, {**ENV, **env}, plog, runs)],
       check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
