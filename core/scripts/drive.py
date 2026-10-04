#!/usr/bin/env python3
"""Driver: composes a run, has its client (scripts/clients.py) build the command, starts it and checks the output.

drive.py --role ROLE [--task TASK] --input FILE|TEXT|- --out FILE --workdir DIR [--repo DIR] [--client NAME]
         [--sid UUID] [--resume] [--dry-run]
Prints the session id on stderr. --dry-run prints {"argv", "cwd", "env"} and starts nothing.
Exits 0 when the run leaves a valid Output frontmatter, 1 when it doesn't, 2 on a config error, 3 when the client fails.
"""
import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clients  # noqa: E402
from compose import ROOT, ConfigError, compose  # noqa: E402

STATUSES = ("done", "needs_input", "failed")


@dataclass
class Access:
    """A run's client-neutral constraints, as absolute paths and exact commands."""
    dirs: list        # extra dirs the run may reach
    read_only: list   # dirs the run may read but never edit
    commands: list    # shell commands to pre-approve


def overlaps(a, b):
    a, b = os.path.realpath(a), os.path.realpath(b)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def bind(entry, repo):
    """A read/write entry as an absolute dir: `repo` is the --repo dir (None without one); else a path."""
    if entry == "repo":
        return os.path.abspath(repo) if repo else None
    return os.path.abspath(os.path.expanduser(entry))


def access(run, root, *, repo, out, workdir):
    """The run's Access; raises ConfigError when a read-only dir overlaps the workdir or the output's dir."""
    workdir, out_dir, root = os.path.abspath(workdir), os.path.dirname(os.path.abspath(out)), os.path.abspath(root)
    reads = [p for p in (bind(e, repo) for e in run["read"]) if p]
    writes = [p for p in (bind(e, repo) for e in run["write"]) if p]
    dirs = [] if out_dir == workdir or out_dir.startswith(workdir + os.sep) else [out_dir]
    dirs += [p for p in reads + writes if p not in dirs]
    read_only = [root] + [p for p in reads if p != root]
    for p in read_only:
        if hit := next((w for w in (workdir, out_dir) if overlaps(p, w)), None):
            raise ConfigError(f"read-only dir {p} overlaps the writable {hit}")
    return Access(dirs, read_only, list(run["commands"]))


def plan(root, role, task=None, *, client, input, out, workdir, repo=None, sid=None, resume=False):
    """The Launch for one run; raises ConfigError."""
    c = clients.get(client, root)
    if resume and not sid:
        raise ConfigError("--resume needs --sid")
    sid = sid or str(uuid.uuid4())
    prompt, run = compose(root, role, task, input=input, out=out, workdir=workdir, resume=resume)
    launch = c.launch(prompt, run, sid=sid, resume=resume, access=access(run, root, repo=repo, out=out, workdir=workdir))
    launch.cwd = os.path.abspath(workdir)
    return launch


def outcome(path):
    """The Output frontmatter's status, or None when the file, the frontmatter or a valid status is missing."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None
    if not text.startswith("---\n") or (end := text.find("\n---", 3)) < 0:
        return None
    for line in text[4:end].splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "status":
            status = value.split("#")[0].strip()
            return status if status in STATUSES else None
    return None


def main(argv, root=ROOT, run=subprocess.run):
    ap = argparse.ArgumentParser(prog="drive.py")
    ap.add_argument("--role", required=True)
    ap.add_argument("--task")
    for flag in ("--input", "--out", "--workdir"):
        ap.add_argument(flag, required=True)
    ap.add_argument("--repo")
    ap.add_argument("--client", default="claude")
    ap.add_argument("--sid")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.input == "-":
        a.input = sys.stdin.read()
    sid = a.sid or (None if a.resume else str(uuid.uuid4()))
    try:
        launch = plan(root, a.role, a.task, client=a.client, input=a.input, out=a.out, workdir=a.workdir,
                      repo=a.repo, sid=sid, resume=a.resume)
    except ConfigError as e:
        print(f"drive.py: {e}", file=sys.stderr)
        return 2
    print(f"drive.py: session {sid}", file=sys.stderr)
    if a.dry_run:
        print(json.dumps({"argv": launch.argv, "cwd": launch.cwd, "env": launch.env}, ensure_ascii=False, indent=1))
        return 0
    os.makedirs(launch.cwd, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    env = {**os.environ, **launch.env}
    if run(launch.argv, cwd=launch.cwd, env=env, stdin=subprocess.DEVNULL).returncode != 0:
        print("drive.py: the client exited nonzero", file=sys.stderr)
        return 3
    status = outcome(a.out)
    if status is None:
        print(f"drive.py: {a.out} has no valid Output frontmatter", file=sys.stderr)
        return 1
    print(f"drive.py: status {status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
