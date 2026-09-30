#!/usr/bin/env python3
"""Research runs: a read-only, detached worktree of the issue's target repo."""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eng import LONG, PLAYGROUND, Q_ISSUE, Invalid, Transient, _stderr, locate, run_subdir, sh_run, target  # noqa: E402
from pipeline import CONFIG, load_config  # noqa: E402

SHA = re.compile(r"[0-9a-f]{40}")

def _commit(wt, src, clone):
    """The detached HEAD of clone's worktree at wt, read from files only, or Invalid."""
    if os.path.islink(wt) or not os.path.isdir(wt) or os.path.realpath(wt) != os.path.join(os.path.realpath(src), os.path.basename(wt)):
        return Invalid(f"{wt} is not a real directory inside {src}")
    try:
        found, _, head = locate(wt)
    except ValueError as e:
        return Invalid(f"{wt}: {e}")
    if os.path.realpath(found) != os.path.realpath(clone):
        return Invalid(f"{wt} is a worktree of {found}, not of {clone}")
    if not SHA.fullmatch(head):
        return Invalid(f"{wt}: HEAD is not a detached commit")
    return head

def prepare(issue_id, gql, run, repos, playground=PLAYGROUND):
    try:
        issue = gql(Q_ISSUE, i=issue_id)["issue"]
    except (SystemExit, Exception) as e:
        return Transient(f"Linear: {e}")
    if issue is None:
        return Invalid(f"{issue_id}: issue not found")
    if issue.get("identifier") != issue_id:
        return Invalid(f"Linear returned {str(issue.get('identifier'))[:40]!r} for {issue_id}")
    t = target(issue, run, playground, repos, push=False)
    if isinstance(t, (Invalid, Transient)):
        return t
    src = run_subdir(issue_id, "src")
    if isinstance(src, Invalid):
        return src
    wt = os.path.join(src, t.name)
    reused = os.path.lexists(wt)
    if not reused:
        for what, argv in (("git fetch", ["git", "-C", t.clone, "fetch", "origin"]),
                           ("git worktree add", ["git", "-c", "core.symlinks=false", "-C", t.clone, "worktree", "add", "--detach", wt, f"origin/{t.default}"])):
            res = run(argv, LONG)
            if res.returncode != 0:
                return Transient(f"{what}: {_stderr(res)}")
    commit = _commit(wt, src, t.clone)
    if isinstance(commit, Invalid):
        return commit
    return {"repo": f"{t.owner}/{t.name}", "mapped": t.mapped, "clone": t.clone, "default": t.default,
            "worktree": wt, "commit": commit, "reused": reused}

def main(argv, env=os.environ, gql=None, run=sh_run, out=sys.stdout, err=sys.stderr, config=CONFIG, playground=PLAYGROUND):
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
        r = prepare(issue, gql, run, cfg["project_repos"], playground)
    except Exception as e:
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
