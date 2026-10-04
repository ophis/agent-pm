#!/usr/bin/env python3
"""Driver: composes a run, has its client (scripts/clients/) build the command, starts it, then checks and saves the
outcome the client reads back.

drive.py --role ROLE [--task TASK] [--input FILE|TEXT|-] --out PATH [--workdir DIR] [--repo DIR] [--client NAME]
         [--sid UUID] [--resume] [--dry-run]
--out is where the deliverable is saved (local and orchestrator destinations), or for a client that only writes files
(skill) the dir it writes under. By default (start's sinks) a run shows its text and progress on stderr and leaves
<workdir>/outcome.json and <workdir>/progress.jsonl.
Prints the session id on stderr. --dry-run prints {"argv", "cwd", "env", "files"} and changes nothing.
Exits 0 when the run returns a valid outcome (or the files are written), 1 when it doesn't, 2 on a config error,
3 when the client fails.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Literal, get_args

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clients  # noqa: E402
from clients import Access, Client, Event, Launch  # noqa: E402
from compose import ROOT, ConfigError, RunConfig, RunParams, fill, load_run, outcome_schema, render  # noqa: E402

Status = Literal["done", "needs_input", "failed"]
STATUSES = get_args(Status)
SAVES_DELIVERABLE = ("local", "orchestrator")   # destinations whose deliverable comes back in the outcome
OUTCOME, PROGRESS = "outcome.json", "progress.jsonl"


class InvalidOutcome(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    status: Status
    title: str
    summary: str
    questions: list[str] = field(default_factory=list)
    url: str = ""
    files: list[str] = field(default_factory=list)
    deliverable: str = ""


@dataclass(frozen=True)
class Result:
    returncode: int
    outcome: Outcome | None   # None: the run returned no valid outcome
    error: str = ""


def bind(entry: str, repo: str | None) -> str | None:
    """A read/write entry as an absolute dir: `repo` is the --repo dir (None without one); else a path."""
    if entry == "repo":
        return os.path.abspath(repo) if repo else None
    return os.path.abspath(os.path.expanduser(entry))


def access(run: RunConfig, params: RunParams, *, repo: str | None, scripts: str) -> Access:
    """The run's Access, `{{scripts}}` and `{{workdir}}` in commands filled. Edit limits are left to the client's
    permission mode (auto)."""
    workdir = os.path.abspath(params.workdir)
    dirs = []
    for p in (bind(e, repo) for e in run.read + run.write):
        if p and p not in dirs:
            dirs.append(p)
    values = {"scripts": scripts, "workdir": workdir}
    return Access(dirs, [fill(c, values, "commands") for c in run.commands])


def plan(root: str, client: Client, role: str, task: str | None = None, *, params: RunParams,
         repo: str | None = None) -> tuple[Launch, RunConfig]:
    """The Launch for one run, with its config; raises ConfigError."""
    if not client.runs:
        raise ConfigError(f"{type(client).__name__} writes files; use export()")
    run = load_run(root, role, task, layers=[client.config])
    prompt = render(root, run, params, vehicle=client)
    acc = access(run, params, repo=repo, scripts=client.scripts_path(root))
    launch = client.launch(prompt, run, params=params, access=acc, schema=outcome_schema(root))
    return launch, run


def export(root: str, client: Client, role: str, task: str | None = None, *, dest: str) -> Launch:
    """The files an export client (skill) writes for role/task under `dest`; raises ConfigError."""
    if client.runs:
        raise ConfigError(f"{type(client).__name__} starts runs; use plan()")
    run = load_run(root, role, task, layers=[client.config])
    return client.export(render(root, run, vehicle=client), run, dest=dest)


def write(files: dict[str, str]) -> None:
    for path, text in files.items():
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


def validate(data: dict, run: RunConfig, params: RunParams) -> Outcome:
    """The outcome a run returned, once its fields hold; raises InvalidOutcome. Its text stays untrusted."""
    if not isinstance(data, dict):
        raise InvalidOutcome("not an object")
    status, title, summary = data.get("status"), data.get("title"), data.get("summary")
    if status not in STATUSES:
        raise InvalidOutcome(f"status {status!r} is not one of {', '.join(STATUSES)}")
    if not isinstance(title, str) or not title.strip() or "\n" in title.strip() or len(title) > 200:
        raise InvalidOutcome("title must be one line of at most 200 characters")
    if not isinstance(summary, str) or len(summary) > 4000:
        raise InvalidOutcome("summary must be text of at most 4000 characters")
    questions = data.get("questions") or []
    if not (isinstance(questions, list) and all(isinstance(q, str) for q in questions)):
        raise InvalidOutcome("questions must be a list of text")
    if status == "needs_input" and not 1 <= len(questions) <= 4:
        raise InvalidOutcome("needs_input must carry 1–4 questions")
    url = _url(data.get("url") or "", run)
    files = [_file(p, params.workdir) for p in data.get("files") or []]
    deliverable = data.get("deliverable") or ""
    if not isinstance(deliverable, str):
        raise InvalidOutcome("deliverable must be text")
    return Outcome(status, title.strip(), summary, questions if status == "needs_input" else [], url, files, deliverable)


def _url(url: str, run: RunConfig) -> str:
    """The url, once it matches what the run's destination can produce."""
    out = run.output
    if not isinstance(url, str):
        raise InvalidOutcome("url must be text")
    if not url or out["type"] in SAVES_DELIVERABLE:
        return ""
    if out["type"] == "github":
        prefix = f"https://{out.get('host', 'github.com')}/{out['repo']}/blob/{out['branch']}/"
        if not url.startswith(prefix):
            raise InvalidOutcome(f"url must start with {prefix}")
    elif not url.startswith("https://") or any(c.isspace() for c in url):
        raise InvalidOutcome("url must be an https link")
    return url


def _file(path: str, workdir: str) -> str:
    """An absolute .md path under the workdir, symlinks resolved."""
    real, base = os.path.realpath(str(path)), os.path.realpath(workdir)
    if not real.endswith(".md") or not real.startswith(base + os.sep):
        raise InvalidOutcome(f"file {str(path)[:200]!r} is not a .md file under the workdir")
    return real

Sink = Callable[[Event], None]   # receives each text and progress event as the run goes, then its checked outcome


def terminal(log=sys.stderr) -> Sink:
    """Shows the run's text and progress, e.g. in its tmux pane."""
    def sink(event: Event) -> None:
        if event.kind == "text":
            print(event.text, file=log, flush=True)
        elif event.kind == "progress":
            print(f"Progress ({event.name}): {event.text}", file=log, flush=True)
    return sink


def progress_file(path: str, *, append: bool = False) -> Sink:
    """Appends each progress report to a JSON-lines file, emptied first unless `append` (a resume)."""
    if not append:
        write({path: ""})

    def sink(event: Event) -> None:
        if event.kind == "progress":
            with open(path, "a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "name": event.name, "text": event.text},
                                   ensure_ascii=False) + "\n")
    return sink


def outcome_file(path: str) -> Sink:
    """Writes the checked outcome as JSON; an earlier one is removed first, so it is never read as this run's."""
    if os.path.exists(path):
        os.remove(path)

    def sink(event: Event) -> None:
        if event.kind == "outcome":
            write({path: json.dumps(event.outcome, ensure_ascii=False, indent=1) + "\n"})
    return sink


def default_sinks(params: RunParams) -> list[Sink]:
    """The terminal, <workdir>/progress.jsonl and <workdir>/outcome.json."""
    workdir = os.path.abspath(params.workdir)
    return [terminal(), progress_file(os.path.join(workdir, PROGRESS), append=params.resume),
            outcome_file(os.path.join(workdir, OUTCOME))]


def start(launch: Launch, run: RunConfig, params: RunParams, *, client: Client, sinks: Sequence[Sink] | None = None,
          popen=subprocess.Popen) -> Result:
    """Starts the run and waits, handing its events to `sinks` (default_sinks() when None); then checks the client's
    last outcome, saves the deliverable to params.out where the destination says so, and hands the outcome on too."""
    write(launch.files)
    workdir = os.path.abspath(params.workdir)
    os.makedirs(launch.cwd or workdir, exist_ok=True)
    os.makedirs(workdir, exist_ok=True)
    sinks = default_sinks(params) if sinks is None else sinks
    raw = None
    proc = popen(launch.argv, cwd=launch.cwd or workdir, env={**os.environ, **launch.env},
                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    for event in client.events(proc.stdout):
        if event.kind == "outcome":
            raw = event.outcome
            continue
        for sink in sinks:
            sink(event)
    rc = proc.wait()
    if rc != 0:
        return Result(rc, None, f"the client exited {rc}")
    if raw is None:
        return Result(rc, None, "the run returned no outcome")
    try:
        outcome = validate(raw, run, params)
    except InvalidOutcome as e:
        return Result(rc, None, f"invalid outcome: {e}")
    if run.output["type"] in SAVES_DELIVERABLE and outcome.deliverable:
        write({os.path.abspath(params.out): outcome.deliverable})
        if run.output["type"] == "local":
            outcome = replace(outcome, url=os.path.abspath(params.out))
    for sink in sinks:
        sink(Event("outcome", outcome=asdict(outcome)))
    return Result(rc, outcome)


def main(argv: list[str], root: str = ROOT, popen=subprocess.Popen) -> int:
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
    params = run = None
    try:
        client = clients.get(a.client, root)
        if client.runs:
            if a.input is None or a.workdir is None:
                raise ConfigError(f"client {a.client!r} needs --input and --workdir")
            params = RunParams(input=a.input, out=a.out, workdir=a.workdir, sid=a.sid, resume=a.resume)
            launch, run = plan(root, client, a.role, a.task, params=params, repo=a.repo)
        else:
            launch = export(root, client, a.role, a.task, dest=a.out)
    except ConfigError as e:
        print(f"drive.py: {e}", file=sys.stderr)
        return 2
    if params:
        print(f"drive.py: session {params.sid}", file=sys.stderr)
    if a.dry_run:
        print(json.dumps({"argv": launch.argv, "cwd": launch.cwd, "env": launch.env, "files": launch.files},
                         ensure_ascii=False, indent=1))
        return 0
    if not params:
        write(launch.files)
        for path in launch.files:
            print(f"drive.py: wrote {path}", file=sys.stderr)
        return 0
    result = start(launch, run, params, client=client, popen=popen)
    if result.outcome is None:
        print(f"drive.py: {result.error}", file=sys.stderr)
        return 3 if result.returncode != 0 else 1
    print(f"drive.py: status {result.outcome.status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
