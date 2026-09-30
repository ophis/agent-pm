#!/usr/bin/env python3
"""Launcher: starts one claude run for an issue the router already claimed (or resumes it), per pipeline.toml.

launch.py --issue ID --url URL --project PROJECT_ID --assignee EMAIL --sid SID --task TASK --mode new|resume [--k K]
The role is the one whose account is EMAIL, the issue's assignee (on a resume, its assignee now); it runs TASK, one of
the role's tasks (the router resolves it). PROJECT_ID fills the prompt's Project: and, for a role whose next role's
default task has repo_from_issue, Project repo:. Every run works in work/<ID>/. Exits 2 for an assignee that is not a
role account or a config error (TASK not one of the role's tasks, a role memory overlapping the issue's repo, a role key
missing from the Keychain, or the docs clone not a directory; logged), 3 when the run cannot start yet (transient: no
transcript to resume, or the Engineering repo step failed transiently). Needs Python 3.11+.
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
from pipeline import (LOGS, PATH, PLACEHOLDERS, PROJECTS, REPO, ROOT, RUNS_LOG, STATES, linear_gql,  # noqa: E402
                      hands_off_to_repo, load_config, overlaps, project_log, role_for, run_dir, runnable, session, transcript)

PRINCIPLES = os.path.join(ROOT, "roles", "principles.md")
# Set inside the tmux command: a running tmux server would otherwise supply its own environment.
ENV = {"PATH": PATH, "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"}  # claude -p otherwise kills a workflow after 10 idle minutes


def prompt(a, run):
    docs = f"{PRINCIPLES}, your role charter {run.charter} and the task {run.instructions}"
    if a.mode == "resume":
        text = (f"Resumed run {a.k} for {a.issue} ({a.url}) after an interruption. Re-read {docs} "
                "first (they may have changed since this session started) and follow the task's resume rule.")
    else:
        text = f"Follow {docs} to handle {a.issue} ({a.url}). The runner has already claimed it."
    if run.memory:
        text += f" Your role memory: {run.memory}; your role charter says how to use it."
    return text


def deny(path):
    """Edit deny rule (covers every file-editing tool) for an absolute path; a single leading "/" would be relative to the project root."""
    return "Edit(//" + path.lstrip("/") + ")"


def command(a, run, tail="", allowed=(), repo=None):
    t, rd = run.task, run_dir(a.issue)
    dirs = [os.path.join(ROOT, d) for d in ("roles", "tasks", "templates")]
    dirs += [os.path.expanduser(d) for d in t.get("add_dirs", [])]
    if run.memory:
        dirs.append(run.memory)
    denied = [os.path.join(ROOT, d, "**") for d in ("roles", "tasks", "templates")]
    denied.append(os.path.join(rd, "worktrees", "*", ".git"))
    for p in run.read_only:
        if p != REPO:
            denied.append(os.path.join(p, "**"))
            continue
        if isinstance(repo, eng.Ok):
            denied.append(os.path.join(repo.clone, "**"))
        denied.append(os.path.join(rd, "worktrees", "**"))
    cmd = ["claude", "-p", prompt(a, run) + tail,
           "--resume" if a.mode == "resume" else "--session-id", a.sid,
           "--model", t["model"], "--effort", t["effort"], "--permission-mode", "auto",
           "--setting-sources", "user", "--strict-mcp-config"]
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


def repo_step(a, task, gql, run, repos):
    """(prompt tail, extra env, allowed rules, repo result) for a repo_from_issue task, or a Transient."""
    try:
        r = eng.resolve(a.issue, gql, run, repos=repos)
    except Exception as e:  # a resolve bug must not crash the launcher: Transient lets Recover retry the issue
        return eng.Transient(f"resolve: {type(e).__name__}: {e}")
    if isinstance(r, eng.Invalid) and a.mode == "resume":
        return eng.Transient(r.reason)  # the launcher never bounces a mid-build run
    if isinstance(r, eng.Transient):
        return r
    cli = f" eng.py: python3 {shlex.quote(os.path.join(ROOT, 'scripts', 'eng.py'))}."
    if isinstance(r, eng.Invalid):
        return f" Repo check failed: {r.reason}.{cli}", {}, [], r
    src = " (from project mapping)" if r.mapped else ""
    tail = (f" Repo check: OK {r.owner}/{r.name}{src}, clone {r.clone}, default branch {r.default}, branch {r.branch}, "
            f"worktree {r.worktree}.{cli}")
    values = {k: getattr(r, k) for k in PLACEHOLDERS}
    return tail, {"AGENT_PM_ISSUE": a.issue}, [t.format(**values) for t in task.get("allowed_tools", [])], r


def main(argv, sh=subprocess.run, config=None, runs=RUNS_LOG, logs=LOGS, gql=None, run=eng.sh_run, projects=PROJECTS, root=ROOT,
         keychain=has_key, docs_ok=os.path.isdir):
    ap = argparse.ArgumentParser(prog="launch.py")
    for f in ("--issue", "--url", "--project", "--assignee", "--sid", "--task"):
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
    jobs = runnable(cfg, root)
    role = role_for(jobs, a.assignee)
    if role is None:
        print(f"launch.py: {a.assignee!r} is not a role account", file=sys.stderr)
        return 2
    job = jobs[role]
    try:
        job = job.with_task(a.task)
    except KeyError:
        return fail(project_log(job.task_name, logs), a.issue, "config-error",
                    f"task {a.task!r} is not one of {role}'s tasks ({', '.join(job.tasks)})", 2)
    plog = project_log(job.task_name, logs)
    if a.mode == "resume":
        path = transcript(a.issue, a.sid, projects)
        if path is None or not os.path.exists(path):
            return fail(plog, a.issue, "transient", f"no transcript to resume at {path}", 3)
    if not keychain(job.key):
        return fail(plog, a.issue, "config-error", f"no Keychain item for role key {job.key}", 2)
    docs = cfg["docs"]
    if not docs_ok(docs["clone"]):
        return fail(plog, a.issue, "config-error", f"docs clone {docs['clone']} is not a directory", 2)
    humans = cfg.get("human_members") or []
    states = ", ".join(f"{STATES[k]}={cfg['states'][k]}" for k in STATES)
    repo_line = f" Project repo: {cfg['project_repos'].get(a.project) or 'none'}." if hands_off_to_repo(cfg, jobs, role) else ""
    tail = (f" Humans: {', '.join(humans) or 'none'}. Project: {a.project}.{repo_line}"
            f" Team: {cfg['team']}. States: {states}."
            f" Docs: {docs['repo']}, clone {docs['clone']}, branch {docs['branch']}.")
    env, allowed, repo = {}, [], None
    if job.task.get("repo_from_issue"):
        step = repo_step(a, job.task, gql or linear_gql, run, cfg["project_repos"])
        if isinstance(step, eng.Transient):
            return fail(plog, a.issue, "transient", step.reason, 3)
        extra, env, allowed, repo = step
        tail += extra
        if job.memory:
            paths = [os.path.join(run_dir(a.issue), "worktrees")] + ([repo.clone] if isinstance(repo, eng.Ok) else [])
            if hit := next((p for p in paths if overlaps(job.memory, p)), None):
                return fail(plog, a.issue, "config-error", f"role memory {job.memory} overlaps the issue's repo {hit}", 2)
    cwd = run_dir(a.issue)
    os.makedirs(cwd, exist_ok=True)
    cmd = command(a, job, tail, allowed, repo)
    sh(["tmux", "new-session", "-d", "-s", session(role), "-c", cwd, "bash", "-c", script(a, cmd, {**ENV, "LINEAR_KEYCHAIN_SERVICE": job.key, **env}, plog, runs)],
       check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
