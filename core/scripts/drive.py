#!/usr/bin/env python3
"""Driver: composes a run, has its client (scripts/clients.py) build the command, starts it and checks the output.

drive.py --role ROLE [--task TASK] [--input FILE|TEXT|-] --out PATH [--workdir DIR] [--repo DIR] [--client NAME]
         [--sid UUID] [--resume] [--dry-run]
--out is the run's Output file, or for a client that only writes files (skill) the dir it writes under.
Prints the session id on stderr. --dry-run prints {"argv", "cwd", "env", "files"} and changes nothing.
Exits 0 when the run leaves a valid Output frontmatter (or the files are written), 1 when it doesn't,
2 on a config error, 3 when the client fails.
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
from compose import ROOT, RUN_KEYS, ConfigError, check, compose, render, resolve_run  # noqa: E402

STATUSES = ("done", "needs_input", "failed")


@dataclass
class Access:
    """A run's client-neutral constraints, as absolute paths and exact commands."""
    dirs: list        # extra dirs the run may reach
    commands: list    # shell commands to pre-approve


def bind(entry, repo):
    """A read/write entry as an absolute dir: `repo` is the --repo dir (None without one); else a path."""
    if entry == "repo":
        return os.path.abspath(repo) if repo else None
    return os.path.abspath(os.path.expanduser(entry))


def access(run, *, repo, out, workdir):
    """The run's Access. Edit limits are left to the client's permission mode (auto)."""
    workdir, out_dir = os.path.abspath(workdir), os.path.dirname(os.path.abspath(out))
    dirs = [] if out_dir == workdir or out_dir.startswith(workdir + os.sep) else [out_dir]
    for p in (bind(e, repo) for e in run["read"] + run["write"]):
        if p and p not in dirs:
            dirs.append(p)
    return Access(dirs, list(run["commands"]))


def override(client, run):
    """The run with the client's own entries for it replacing the neutral ones (any RUN_KEYS); raises ConfigError."""
    for key in RUN_KEYS:
        if (value := client.value(run, key)) is not None:
            run[key] = value
    return check(run)


def plan(root, role, task=None, *, client, input, out, workdir, repo=None, sid=None, resume=False):
    """The Launch for one run; raises ConfigError."""
    c = clients.get(client, root)
    if not c.runs:
        run = override(c, resolve_run(root, role, task))
        return c.launch(render(root, run), run, sid=None, resume=False, access=Access([], list(run["commands"])), out=out)
    if input is None or workdir is None:
        raise ConfigError(f"client {client!r} needs --input and --workdir")
    if resume and not sid:
        raise ConfigError("--resume needs --sid")
    sid = sid or str(uuid.uuid4())
    run = override(c, resolve_run(root, role, task))
    prompt = render(root, run, input=input, out=out, workdir=workdir, resume=resume)
    acc = access(run, repo=repo, out=out, workdir=workdir)
    launch = c.launch(prompt, run, sid=sid, resume=resume, access=acc, out=out)
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
    ap.add_argument("--input")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workdir")
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
    if launch.argv:
        print(f"drive.py: session {sid}", file=sys.stderr)
    if a.dry_run:
        print(json.dumps({"argv": launch.argv, "cwd": launch.cwd, "env": launch.env, "files": launch.files},
                         ensure_ascii=False, indent=1))
        return 0
    for path, text in launch.files.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        print(f"drive.py: wrote {path}", file=sys.stderr)
    if not launch.argv:
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
