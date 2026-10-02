#!/usr/bin/env python3
"""Research runs: a read-only, detached worktree of the issue's target repo."""
import json, os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eng import LONG, PLAYGROUND, Invalid, Transient, _stderr, locate, read_issue, run_subdir, sh_run, target  # noqa: E402
from pipeline import CONFIG, load_config, run_dir  # noqa: E402

SHA = re.compile(r"[0-9a-f]{40}")
FETCH_TRIES, FETCH_WAIT = 4, 5   # a run fetching the shared clone at the same time holds a ref lock; it is gone seconds later

def _worktree(wt, clone):
    """(HEAD text, gitdir) of clone's worktree at wt, read from files only, or Invalid."""
    if os.path.islink(wt) or not os.path.isdir(wt):
        return Invalid(f"{wt} is not a real directory")
    try:
        found, _, head, gitdir = locate(wt)
    except ValueError as e:
        return Invalid(f"{wt}: {e}")
    if os.path.realpath(found) != os.path.realpath(clone):
        return Invalid(f"{wt} is a worktree of {found}, not of {clone}")
    return head, gitdir

def prepare(issue_id, gql, run, repos, playground=PLAYGROUND, sleep=time.sleep):
    issue = read_issue(issue_id, gql)
    if isinstance(issue, (Invalid, Transient)):
        return issue
    t = target(issue, run, playground, repos, push=False)
    if isinstance(t, (Invalid, Transient)):
        return t
    src = run_subdir(issue_id, "src")
    if isinstance(src, Invalid):
        return src
    wt = os.path.join(src, t.name)
    real = os.path.realpath(wt)
    if real != os.path.join(os.path.realpath(run_dir(issue_id)), "src", t.name):
        return Invalid(f"{wt} resolves to {real}")
    found = _worktree(wt, t.clone) if os.path.lexists(wt) else None
    if isinstance(found, Invalid):
        return found
    steps = []
    # git keeps a new worktree's gitdir locked until `worktree add` completes.
    if found and os.path.lexists(os.path.join(found[1], "locked")):
        steps.append(("git worktree remove", ["git", "-C", t.clone, "worktree", "remove", "--force", "--force", "--", wt]))
    if not found or steps:
        steps += [("git fetch", ["git", "-C", t.clone, "fetch", "origin"]),
                  ("git worktree add", ["git", "-c", "core.symlinks=false", "-C", t.clone, "worktree", "add", "--detach", wt, f"origin/{t.default}"])]
    for what, argv in steps:
        tries = FETCH_TRIES if what == "git fetch" else 1
        for attempt in range(tries):
            if attempt:
                sleep(FETCH_WAIT)
            res = run(argv, LONG)
            if res.returncode == 0:
                break
        if res.returncode != 0:
            return Transient(f"{what}: {_stderr(res)}")
    if steps:
        found = _worktree(wt, t.clone)
        if isinstance(found, Invalid):
            return found
    if not SHA.fullmatch(found[0]):
        return Invalid(f"{wt}: HEAD is not a detached commit")
    return {"repo": f"{t.owner}/{t.name}", "mapped": t.mapped, "clone": t.clone, "default": t.default,
            "worktree": wt, "commit": found[0], "reused": not steps}

def main(argv, env=os.environ, gql=None, run=sh_run, out=sys.stdout, err=sys.stderr, config=CONFIG, playground=PLAYGROUND,
         sleep=time.sleep):
    import argparse
    ap = argparse.ArgumentParser(prog="research.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    ap.parse_args(argv)
    issue = env.get("AGENT_PM_ISSUE", "")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", issue):
        err.write("research.py: AGENT_PM_ISSUE is not set to an issue id\n")
        return 1
    try:
        cfg = load_config(config)
    except SystemExit as e:
        err.write(f"research.py: {e.code}\n")
        return 1
    if gql is None:
        from pipeline import linear_gql as gql
    try:
        r = prepare(issue, gql, run, cfg["project_repos"], playground, sleep)
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as e:
        err.write(f"research.py: prepare: {e!r}\n")
        return 1
    if isinstance(r, (Invalid, Transient)):
        err.write(f"research.py: {r.reason}\n")
        return 2 if isinstance(r, Invalid) else 1
    json.dump(r, out)
    out.write("\n")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
