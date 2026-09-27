#!/usr/bin/env python3
"""Launcher: starts one claude run for an issue the router already claimed (or resumes it), per pipeline.toml.

launch.py --issue ID --url URL --project NAME --sid SID --mode new|resume [--k K]
Exits 2 for an unknown or non-runnable project. Needs Python 3.11+.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import PATH, ROOT, RUNS_LOG, SESSION, WORK, load_config, project_log, runnable  # noqa: E402

# Set inside the tmux command: a running tmux server would otherwise supply its own environment.
ENV = {"PATH": PATH, "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"}  # claude -p otherwise kills a workflow after 10 idle minutes


def prompt(a, instructions):
    if a.mode == "resume":
        return (f"Resumed run {a.k} for {a.issue} ({a.url}) after an interruption. Re-read {instructions} first "
                "(it may have changed since this session started) and follow its resume rule.")
    return f"Follow {instructions} to handle {a.issue} ({a.url}). The runner has already claimed it."


def command(a, p):
    instructions = os.path.join(ROOT, p["instructions"])
    cmd = ["claude", "-p", prompt(a, instructions), "--resume" if a.mode == "resume" else "--session-id", a.sid,
           "--model", p["model"], "--effort", p["effort"], "--permission-mode", "auto", "--add-dir", ROOT]
    for d in p.get("add_dirs", []):
        cmd += ["--add-dir", os.path.expanduser(d)]
    return cmd


def script(a, p, plog, runs):
    q = shlex.quote
    env = " ".join(f"{k}={q(v)}" for k, v in ENV.items())
    end = f'"$(date "+%F %T") end {a.issue} session={a.sid} exit=$rc"'
    return (f"export {env}; "
            f'echo "$(date "+%F %T") launch {a.issue} mode={a.mode} session={a.sid}" >> {q(plog)}; '
            f"{shlex.join(command(a, p))} < /dev/null 2>&1 | tee -a {q(plog)}; rc=${{PIPESTATUS[0]}}; "
            f"echo {end} >> {q(plog)}; echo {end} >> {q(runs)}")


def main(argv, sh=subprocess.run, config=None, runs=RUNS_LOG, logs=None):
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
    projects = runnable(load_config(config) if config else load_config())
    if a.project not in projects:
        print(f"launch.py: {a.project!r} is not a runnable project in pipeline.toml", file=sys.stderr)
        return 2
    plog = project_log(a.project, logs) if logs else project_log(a.project)
    os.makedirs(WORK, exist_ok=True)
    sh(["tmux", "new-session", "-d", "-s", SESSION, "-c", WORK, "bash", "-c", script(a, projects[a.project], plog, runs)],
       check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
