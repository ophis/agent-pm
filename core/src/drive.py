#!/usr/bin/env python3
"""Driver: composes a run, has its client (src/clients/) build the command, starts it, then checks and saves the
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
import posixpath
import re
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clients  # noqa: E402
from clients import Access, Client, Event, Launch  # noqa: E402
from compose import ROOT, ConfigError, RunConfig, RunParams, fill, load_run, outcome_schema, render  # noqa: E402

Status = Literal["done", "needs_input", "failed"]
STATUSES = get_args(Status)
SAVES_DELIVERABLE = ("local", "orchestrator")   # destinations whose deliverable comes back in the outcome
OUTCOME, PROGRESS = "outcome.json", "progress.jsonl"
PR_PATH = re.compile(r"/[^/]+/[^/]+/(pull/\d+|compare/\S+|tree/\S+)")


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


# The run controls its workdir, where the driver writes too: writes replace a symlink planted at the path, never
# follow it.
def save(path: str | Path, text: str) -> None:
    """Writes `text` to `path` atomically, through a temp file renamed over it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".tmp-", delete=False) as f:
        f.write(text)
    os.replace(f.name, path)


def append_line(path: str | Path, text: str) -> None:
    """Appends to `path`, refusing a symlink."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644), "a") as f:
        f.write(text)


def printable(text: str) -> str:
    """`text` without control characters but tabs, so a run's output can't steer the terminal."""
    return "".join(c for c in text if c.isprintable() or c == "\t")


def validate(data: dict, run: RunConfig, params: RunParams) -> Outcome:
    """The outcome a run returned, once it holds; raises InvalidOutcome. Shape and lengths are the schema's
    (output/outcome.schema.json), which the client enforces; this checks what the schema can't. Its text stays
    untrusted."""
    if not isinstance(data, dict):
        raise InvalidOutcome("not an object")
    status, title, summary = data.get("status"), data.get("title"), data.get("summary")
    questions, files, deliverable = data.get("questions") or [], data.get("files") or [], data.get("deliverable") or ""
    if status not in STATUSES:
        raise InvalidOutcome(f"status {status!r} is not one of {', '.join(STATUSES)}")
    if not isinstance(title, str) or not title.strip() or not title.isprintable():
        raise InvalidOutcome("title must be one line of printable text")
    if not isinstance(summary, str) or not isinstance(deliverable, str):
        raise InvalidOutcome("summary and deliverable must be text")
    if not isinstance(questions, list) or not isinstance(files, list):
        raise InvalidOutcome("questions and files must be lists")
    if status == "needs_input" and not 1 <= len(questions) <= 4:
        raise InvalidOutcome("needs_input must carry 1–4 questions")
    if status == "done" and run.output["type"] in SAVES_DELIVERABLE and not deliverable.strip():
        raise InvalidOutcome(f"done with a {run.output['type']} destination must carry the deliverable")
    return Outcome(status, title.strip(), summary, [str(q) for q in questions] if status == "needs_input" else [],
                   _url(data.get("url") or "", run), [_file(p, params.workdir) for p in files], deliverable)


def _url(url: str, run: RunConfig) -> str:
    """The url, once it is a plain https link the run's destination can produce."""
    out = run.output
    if not url or out["type"] in SAVES_DELIVERABLE:
        return ""
    parts = urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme != "https" or not parts.netloc or not url.isprintable() or " " in url
            or posixpath.normpath(unquote(parts.path)) != unquote(parts.path)):
        raise InvalidOutcome("url must be a plain https link")
    if out["type"] == "github":
        host, prefix = out.get("host", "github.com"), f"/{out['repo']}/blob/{out['branch']}/"
        if parts.netloc != host or not parts.path.startswith(prefix):
            raise InvalidOutcome(f"url must start with https://{host}{prefix}")
    elif out["type"] == "pull-request" and not PR_PATH.fullmatch(parts.path):
        raise InvalidOutcome("url must be a pull request, compare or tree link")
    return url


def _file(path, workdir: str) -> str:
    """A regular .md file under the workdir with one link, symlinks resolved; a relative path is the workdir's."""
    base = Path(workdir).resolve()
    real = (base / path).resolve() if isinstance(path, str) else None
    # A hard link is the same file under another name: it could carry, e.g., a key into the workdir.
    if (real is None or real.suffix != ".md" or not real.is_relative_to(base) or not real.is_file()
            or real.stat().st_nlink != 1):
        raise InvalidOutcome(f"file {str(path)[:200]!r} is not a single-link .md file under the workdir")
    return str(real)

Sink = Callable[[Event], None]   # receives each text and progress event as the run goes, then its checked outcome


def terminal(log=sys.stderr) -> Sink:
    """Shows the run's text and progress, e.g. in its tmux pane."""
    def sink(event: Event) -> None:
        if event.kind == "text":
            print(printable(event.text), file=log, flush=True)
        elif event.kind == "progress":
            print(printable(f"Progress ({event.name}): {event.text}"), file=log, flush=True)
    return sink


def progress_file(path: str, *, append: bool = False) -> Sink:
    """Appends each progress report to a JSON-lines file, emptied first unless `append` (a resume)."""
    if not append:
        save(path, "")

    def sink(event: Event) -> None:
        if event.kind == "progress":
            line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "name": event.name, "text": event.text}
            append_line(path, json.dumps(line, ensure_ascii=False) + "\n")
    return sink


def outcome_file(path: str) -> Sink:
    """Writes the checked outcome as JSON; an earlier one is removed first, so it is never read as this run's."""
    Path(path).unlink(missing_ok=True)

    def sink(event: Event) -> None:
        if event.kind == "outcome":
            save(path, json.dumps(event.outcome, ensure_ascii=False, indent=1) + "\n")
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
    out = Path(params.out).absolute()
    if not out.is_dir():
        out.unlink(missing_ok=True)   # an earlier deliverable is never read as this run's
    raw = None
    proc = popen(launch.argv, cwd=launch.cwd or workdir, env={**os.environ, **launch.env},
                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    try:
        for event in client.events(proc.stdout):
            if event.kind == "outcome":
                raw = event.outcome
                continue
            for sink in sinks:
                sink(event)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
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
        save(out, outcome.deliverable)
        if run.output["type"] == "local":
            outcome = replace(outcome, url=str(out))
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
