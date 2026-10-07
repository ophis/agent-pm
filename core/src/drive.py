#!/usr/bin/env python3
"""Driver: composes an agent run, has its client (src/clients/) build the command, starts it through a runner, tails the
channel the run reports its progress and outcome to (report.py, pre-approved for every run), then checks and saves the
outcome.

drive.py --role ROLE [--task TASK] --input FILE|TEXT|- --out PATH --workdir DIR [--repo DIR] [--client NAME]
         [--sid UUID] [--resume] [--runner headless|tui] [--split right|below] [--beside SESSION] [--prefix PREFIX]
         [--events FILE] [--dry-run]
drive.py --client skill --role ROLE [--task TASK]
--out is where the deliverable is saved (local and orchestrator destinations). The run starts in the `cwd` run key,
unset → the caller's current directory; a resume in its session's recorded one (place()). By default (start's sinks) a
run shows its text and progress on stderr; every run leaves the record <workdir>/run.json (Record).
--runner hosts the run: headless (default) runs the client's command on a pipe until it exits; tui runs its interactive
command in a detached tmux session <prefix>-<sid[:8]> (tui_claude.py; --prefix, default <role>-<task>), shown as the
`show` run key says (by default stacked with the panes of the same opener; --split/--beside place it explicitly), done
once the outcome arrives or it gives up, and left open for the user. --events FILE appends the session's state events
there. --split, --beside, --prefix and --events are tui only.
The skill client starts nothing: it prints the role/task's prompt on stdout for the calling Claude Code conversation to
follow (the act-as skill), its paths this core's.
Prints the session id on stderr. --dry-run prints {"argv" (the runner's command), "cwd", "env"} and changes nothing.
Exits 0 when the run returns a valid outcome (or the prompt is printed), 1 when it doesn't, 2 on a config error, 3 when
the client or its tmux session fails.
"""
import argparse
import errno
import json
import os
import queue
import subprocess
import posixpath
import re
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol, get_args
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clients  # noqa: E402
import repo as repos  # noqa: E402
import tui_claude  # noqa: E402
from clients import Access, Client, Event, Launch  # noqa: E402
from compose import (CONFIG, ROOT, ConfigError, RunConfig, RunParams, fill, load_run, outcome_schema,  # noqa: E402
                     render, report_command)

Status = Literal["done", "needs_input", "failed"]
STATUSES = get_args(Status)
SAVES_DELIVERABLE = ("local", "orchestrator")   # destinations whose deliverable comes back in the outcome
RECORD = "run.json"   # in the workdir: the driver's record of the agent run (Record)
NAME = re.compile(r"[\w-]+")
POLL = 0.5   # seconds between reads of the channel while the host is quiet
WAIT_LIMIT = 2 * 60 * 60   # seconds the tui runner waits for an outcome or a progress report before it gives up
STOP_LIMIT = 3   # turn ends with no outcome and no pending work, after its nudge, before the tui runner gives up
NUDGE = ("Finish your task, then report its outcome with the report command your instructions name. "
         "If you are waiting for background work, wait for it first.")
PR_PATH = re.compile(r"/[^/]+/[^/]+/(pull/\d+|compare/\S+|tree/\S+)")


class InvalidOutcome(Exception):
    pass


class RunnerError(Exception):
    """A runner's host failed: the agent run ends with no outcome, rc 1."""


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
    outcome: Outcome | None   # None: the agent run returned no valid outcome
    error: str = ""


def bind(entry: str, repo: str | None) -> str | None:
    """A read/write entry as an absolute dir: `repo` is the --repo dir (None without one); else a path."""
    if entry == "repo":
        return os.path.abspath(repo) if repo else None
    return os.path.abspath(os.path.expanduser(entry))


def access(run: RunConfig, params: RunParams, *, repo: str | None, scripts: str, methods: str, cwd: str | None = None,
           project: bool = False) -> Access:
    """The agent run's Access, `{{methods}}` in read/write entries and `{{scripts}}` and `{{workdir}}` in commands
    filled, then the report command and the gate (used verbatim) pre-approved too; the workdir is the first dir when
    `cwd` (default the workdir) is another. Edit limits are left to the client's permission mode (auto)."""
    workdir = os.path.abspath(params.workdir)
    cwd = cwd or workdir
    dirs = [workdir] if os.path.realpath(cwd) != os.path.realpath(workdir) else []
    for key, entries in (("read", run.read), ("write", run.write)):
        for entry in entries:
            p = bind(fill(entry, {"methods": methods}, key), repo)
            if p and p not in dirs:
                dirs.append(p)
    values = {"scripts": scripts, "workdir": workdir}
    commands = [fill(c, values, "commands") for c in run.commands] + [f"{report_command(scripts, params)} *"]
    return Access(dirs, commands + ([run.gate] if run.gate else []), cwd=cwd, project=project)


def trusted_dirs(root: str) -> frozenset[str]:
    """Real paths of the global `trusted_dirs` in <root>'s config (repo.read_config); ConfigError when it is malformed."""
    try:
        return repos.trusted_dirs(repos.read_config(os.path.join(root, CONFIG)))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def status_line(root: str) -> bool:
    """The global `status_line` in <root>'s config (repo.read_config); ConfigError when it is malformed."""
    try:
        return repos.status_line(repos.read_config(os.path.join(root, CONFIG)))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def place(root: str, run: RunConfig, params: RunParams, cwd: str | None = None) -> tuple[str, bool]:
    """(the agent run's cwd, whether its client loads the cwd's project settings and instructions). A new run's cwd is
    the `cwd` run key, else `cwd`, else the caller's current directory; project is on when trusted_dirs holds its real
    path or a parent. A resume reuses its session's record (none: the workdir, project off). ConfigError when the cwd
    is no directory, or project would be on with the cwd in the workdir or no longer trusted. An untrusted cwd other
    than the workdir gets a stderr notice."""
    workdir, trusted = os.path.abspath(params.workdir), trusted_dirs(root)
    if params.resume:
        entry = session(workdir, params.sid)
        here, project = (entry["cwd"], entry["project"]) if entry else (workdir, False)
        if project and not repos.under(os.path.realpath(here), trusted):
            raise ConfigError(f"cwd {here} is no longer in trusted_dirs")
    else:
        here = os.path.abspath(os.path.expanduser(run.cwd or cwd or os.getcwd()))
        project = repos.under(os.path.realpath(here), trusted) is not None
    real, real_work = os.path.realpath(here), os.path.realpath(workdir)
    if real != real_work and not os.path.isdir(here):
        raise ConfigError(f"cwd {here} is not a directory")
    if project and repos.under(real, [real_work]):
        raise ConfigError(f"cwd {here} is in trusted_dirs and in the workdir: project settings must not load from it")
    if not project and real != real_work:
        print(f"drive.py: cwd {here} is not in trusted_dirs: its project settings are off", file=sys.stderr)
    return here, project


def plan(root: str, client: Client, role: str, task: str | None = None, *, params: RunParams,
         repo: str | None = None, layers: Sequence[Mapping] = (), cwd: str | None = None) -> tuple[Launch, RunConfig]:
    """The Launch for one agent run, with its config; raises ConfigError. `layers` (config.toml's layout) apply after
    the client's config; `cwd` is the cwd when the run key is unset (place())."""
    if not client.runs:
        raise ConfigError(f"{type(client).__name__} prints a prompt; use inline()")
    run = load_run(root, role, task, layers=[client.config, *layers])
    here, project = place(root, run, params, cwd)
    prompt = render(root, run, params, client=client)
    acc = access(run, params, repo=repo, scripts=client.scripts_path(root), methods=client.methods_path(root), cwd=here,
                 project=project)
    launch = client.launch(prompt, run, params=params, access=acc)
    return replace(launch, project=project, status_line=status_line(root)), run


def inline(root: str, client: Client, role: str, task: str | None = None) -> str:
    """The prompt an inline client (skill) gives for role/task; raises ConfigError."""
    if client.runs:
        raise ConfigError(f"{type(client).__name__} starts agent runs; use plan()")
    run = load_run(root, role, task, layers=[client.config])
    return client.inline(render(root, run, client=client))


# The agent run controls its workdir, where the driver writes too: writes replace a symlink planted at the path, never
# follow it.
def save(path: str | Path, text: str) -> None:
    """Writes `text` to `path` atomically, through a temp file renamed over it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".tmp-", delete=False) as f:
        try:
            f.write(text)
            f.close()
            os.replace(f.name, path)
        except BaseException:
            os.unlink(f.name)
            raise


def append_line(path: str | Path, text: str) -> None:
    """Appends to `path`, refusing a symlink or anything but a regular file (a FIFO would block)."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", str(path))
    with os.fdopen(fd, "a") as f:
        f.write(text)


def printable(text: str) -> str:
    """`text` without control characters but tabs, so an agent run's output can't steer the terminal."""
    return "".join(c for c in text if c.isprintable() or c == "\t")


def validate(data: dict, run: RunConfig, params: RunParams) -> Outcome:
    """The outcome an agent run returned, once it holds; raises InvalidOutcome. Checks the schema
    (output/outcome.schema.json) and what it can't. Its text stays untrusted."""
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
    outcome = Outcome(status, title.strip(), summary, [str(q) for q in questions] if status == "needs_input" else [],
                      _url(data.get("url") or "", run), [_file(p, params.workdir) for p in files], deliverable)
    _conform(data, outcome_schema(ROOT), "outcome")
    return outcome


def _conform(value, schema: Mapping, where: str) -> None:
    """`value` against the subset of JSON Schema output/outcome.schema.json uses."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise InvalidOutcome(f"{where} must be an object")
        props = schema.get("properties", {})
        if missing := [k for k in schema.get("required", ()) if k not in value]:
            raise InvalidOutcome(f"{where} lacks {missing[0]!r}")
        if schema.get("additionalProperties") is False and (extra := sorted(set(value) - set(props))):
            raise InvalidOutcome(f"unknown field {extra[0][:100]!r}")
        for k, v in value.items():
            if k in props:
                _conform(v, props[k], k)
    elif kind == "string":
        if not isinstance(value, str):
            raise InvalidOutcome(f"{where} must be text")
        if len(value) < schema.get("minLength", 0):
            raise InvalidOutcome(f"{where} is shorter than {schema['minLength']} characters")
        if len(value) > schema.get("maxLength", len(value)):
            raise InvalidOutcome(f"{where} is longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise InvalidOutcome(f"{where} must match {schema['pattern']}")
    elif kind == "array":
        if not isinstance(value, list):
            raise InvalidOutcome(f"{where} must be a list")
        if len(value) > schema.get("maxItems", len(value)):
            raise InvalidOutcome(f"{where} has more than {schema['maxItems']} items")
        for i, v in enumerate(value):
            _conform(v, schema.get("items", {}), f"{where}[{i}]")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidOutcome(f"{where} {value!r} is not one of {', '.join(map(str, schema['enum']))}")


def _url(url: str, run: RunConfig) -> str:
    """The url, once it is a plain https link the agent run's destination can produce."""
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

Sink = Callable[[Event], None]   # receives each text and progress event as the agent run goes, then a missing mark and the checked outcome


def terminal(log=sys.stderr) -> Sink:
    """Shows the agent run's text and progress, e.g. in its tmux pane."""
    def sink(event: Event) -> None:
        if event.kind == "text":
            print(printable(event.text), file=log, flush=True)
        elif event.kind == "progress":
            print(printable(f"Progress ({event.name}): {event.text}"), file=log, flush=True)
    return sink


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def record(workdir: str) -> dict:
    """<workdir>/run.json as written by Record; {} when it is missing or no JSON object. A symlink or anything but a
    regular file at the path is never read."""
    try:
        fd = os.open(os.path.join(workdir, RECORD), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return {}
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return {}
        try:
            data = json.loads(f.read())
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def session(workdir: str, sid: str) -> dict | None:
    """The record's entry for session `sid`, when it names a printable absolute cwd and project as a bool; else None.
    The agent run can write its workdir: no other field is checked."""
    entries = record(workdir).get("sessions")
    for e in entries if isinstance(entries, list) else ():
        if (isinstance(e, dict) and e.get("sid") == sid and isinstance(e.get("cwd"), str) and os.path.isabs(e["cwd"])
                and e["cwd"].isprintable() and isinstance(e.get("project"), bool)):
            return e
    return None


class Record:
    """<workdir>/run.json, written only here, each write replacing the file (save): `sessions`, one entry per session
    (sid, cwd, project, transcript, resume, started, ended; a resume keeps its entry's started), the
    `progress` reports (a new session starts them anew) and the last validated `outcome` (null until one holds)."""
    def __init__(self, launch: Launch, params: RunParams):
        workdir = os.path.abspath(params.workdir)
        self.path, old = os.path.join(workdir, RECORD), record(workdir)
        entries = [e for e in old.get("sessions") or [] if isinstance(e, dict)] if isinstance(old.get("sessions"), list) else []
        prior = session(workdir, params.sid) if params.resume else None
        entry = {"sid": params.sid, "cwd": launch.cwd or workdir, "project": launch.project,
                 "transcript": launch.transcript, "resume": launch.resume, "started": stamp(), "ended": None}
        if prior and isinstance(prior.get("started"), str):
            entry["started"] = prior["started"]
        at = next((i for i, e in enumerate(entries) if e.get("sid") == params.sid), len(entries))
        entries[at:at + 1] = [entry]
        progress = old.get("progress") if params.resume and isinstance(old.get("progress"), list) else []
        self.data, self.entry = {"sessions": entries, "progress": progress, "outcome": None}, entry
        self.write()

    def write(self) -> None:
        save(self.path, json.dumps(self.data, ensure_ascii=False, indent=1) + "\n")

    def progress(self, event: Event) -> None:
        self.data["progress"].append({"ts": stamp(), "name": event.name, "text": event.text})
        self.write()

    def outcome(self, outcome: dict) -> None:
        self.data["outcome"] = outcome
        self.write()

    def end(self) -> None:
        self.entry["ended"] = stamp()
        self.write()


def report_event(line: str) -> Event | None:
    """A channel line as its Event; None unless it is an outcome object, a turn end or a progress report named by a
    word with text, its lines joined into one."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("kind") == "outcome" and isinstance(data.get("outcome"), dict):
        return Event("outcome", outcome=data["outcome"])
    if data.get("kind") == "stop":
        pending = data.get("pending")
        return Event("stop", pending=pending if type(pending) is int and pending >= 0 else 0)
    name, text = data.get("name"), data.get("text")
    if data.get("kind") == "progress" and isinstance(name, str) and NAME.fullmatch(name) and isinstance(text, str):
        if text := " ".join(text.split()):
            return Event("progress", text, name=name)
    return None


class Tail:
    """The reports appended to a channel since this was made; a symlink at the path is never read."""
    def __init__(self, path: str):
        self.path, self.rest = path, b""
        try:
            st = os.lstat(path)
            self.pos = st.st_size if stat.S_ISREG(st.st_mode) else 0
        except FileNotFoundError:
            self.pos = 0

    def __call__(self) -> Iterator[Event]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ELOOP):
                return
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return
        with os.fdopen(fd, "rb") as f:
            if os.fstat(f.fileno()).st_size < self.pos:   # replaced or emptied: read it anew
                self.pos, self.rest = 0, b""
            f.seek(self.pos)
            data = f.read()
        self.pos += len(data)
        *lines, self.rest = (self.rest + data).split(b"\n")
        for line in lines:
            if event := report_event(line.decode("utf-8", "replace")):
                yield event


TUI_SESSION = re.compile(r"(?P<prefix>[A-Za-z0-9_-]+)-[0-9a-f]{8}")


def tui_session(role: str, task: str, sid: str, prefix: str | None = None) -> str:
    """The name of the tui runner's tmux session for an agent run: `prefix` (default <role>-<task>), then the sid's
    first 8 characters."""
    return f"{prefix or f'{role}-{task}'}-{sid[:8]}"


@dataclass(frozen=True)
class Layout:
    """The tui runner's default show: the split's side (tui_claude.SPLITS), the tmux session whose pane it splits, and
    the opener whose panes it stacks with (tui_claude.OPENER; None: the caller's own). With split and beside both None
    the pane goes by the stacking rule (tui_claude.py)."""
    split: str | None = None
    beside: str | None = None
    opener: str | None = None


class Runner(Protocol):
    """How an agent run's command is hosted, and when the run counts as done; the driver loop is the same for every
    runner."""
    starts: str   # the Launch field holding the command it starts

    # begin and poll raise RunnerError when the host fails.
    def begin(self, argv: list[str], *, cwd: str, env: dict[str, str]) -> None: ...
    def poll(self, timeout: float) -> tuple[list[Event], bool]: ...   # the host's own events; whether it ended
    def seen(self, event: Event) -> None: ...   # each channel event, before the sinks get it
    def done(self, outcome_arrived: bool) -> bool: ...   # finished while the host still runs
    def returncode(self) -> int: ...
    def stop(self) -> None: ...   # on a driver exception or a RunnerError, begin's too


class Headless:
    """The client's command on a pipe, its stdout turned into events by the client; ended at EOF."""
    starts = "argv"

    def __init__(self, *, run: RunConfig, params: RunParams, client: Client, popen, layout: Layout | None,
                 prefix: str | None = None, events: str | None = None, status_line: bool = False):
        self.client, self.popen, self.proc = client, popen, None

    def begin(self, argv: list[str], *, cwd: str, env: dict[str, str]) -> None:
        self.proc = self.popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
        self.stdout = queue.Queue()
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self) -> None:
        try:
            for event in self.client.events(self.proc.stdout):
                self.stdout.put(event)
        except BaseException as e:
            self.stdout.put(e)
        else:
            self.stdout.put(None)

    def poll(self, timeout: float) -> tuple[list[Event], bool]:
        try:
            item = self.stdout.get(timeout=timeout)
        except queue.Empty:
            return [], False
        if isinstance(item, BaseException):
            raise item
        return ([], True) if item is None else ([item], False)

    def seen(self, event: Event) -> None:
        pass

    def done(self, outcome_arrived: bool) -> bool:
        return False

    def returncode(self) -> int:
        return self.proc.wait()

    def stop(self) -> None:
        if self.proc is not None:   # None: popen failed
            self.proc.kill()
            self.proc.wait()


class Tui:
    """The client's interactive command in a detached tmux session (tui_claude.py), never killed after the outcome. Done once
    the outcome arrives, or once it gives up and leaves the session to a human. A turn end without an outcome
    (`stop`) with background work pending is ignored; any other gets one NUDGE, and STOP_LIMIT more, counted since the
    last progress report, give up. So does WAIT_LIMIT seconds since the last progress report, or the agent run's start,
    without an outcome."""
    starts = "interactive"

    def __init__(self, *, run: RunConfig, params: RunParams, client: Client, popen, layout: Layout | None,
                 prefix: str | None = None, events: str | None = None, status_line: bool = False):
        self.run, self.layout, self.events, self.status_line = run, layout or Layout(), events, status_line
        self.name = tui_session(run.role, run.task, params.sid, prefix)
        self.rc, self.outcome, self.nudged, self.stops, self.gave_up = 0, False, False, 0, False
        self.started, self.since = False, 0.0

    def begin(self, argv: list[str], *, cwd: str, env: dict[str, str]) -> None:
        try:
            tui_claude.start(self.name, argv, cwd=cwd, env=env, events=self.events, template=self.run.show,
                             split=self.layout.split, beside=self.layout.beside, opener=self.layout.opener,
                             status_line=self.status_line)
        except tui_claude.TuiError as e:
            raise RunnerError(str(e)) from e
        self.started, self.since = True, time.monotonic()

    def poll(self, timeout: float) -> tuple[list[Event], bool]:
        time.sleep(timeout)
        try:
            state = tui_claude.status(self.name)
        except tui_claude.TuiError as e:
            raise RunnerError(str(e)) from e
        if state == tui_claude.RUNNING:
            return [], False
        self.rc = state or 0   # None: the session is gone
        return [], True

    def seen(self, event: Event) -> None:
        if event.kind == "outcome":
            self.outcome = True
        elif event.kind == "progress":
            self.stops, self.since = 0, time.monotonic()
        elif event.kind == "stop" and not (self.outcome or self.gave_up or event.pending):
            if not self.nudged:
                self.nudged = True
                try:
                    tui_claude.send(self.name, NUDGE)
                except tui_claude.TuiError as e:
                    print(f"drive.py: tui: {e}", file=sys.stderr)
                return
            self.stops += 1
            if self.stops >= STOP_LIMIT:
                self.give_up(f"no outcome after {self.stops} stops")

    def give_up(self, reason: str) -> None:
        self.gave_up = True
        print(f"drive.py: {reason}; session {self.name} left open: {tui_claude.attach_command(self.name)}", file=sys.stderr)

    def done(self, outcome_arrived: bool) -> bool:
        if outcome_arrived:
            return True
        if not self.gave_up and time.monotonic() - self.since > WAIT_LIMIT:
            self.give_up(f"no outcome {WAIT_LIMIT / 3600:g} h after the last progress report")
        return self.gave_up

    def returncode(self) -> int:
        return 0 if self.outcome else self.rc

    def stop(self) -> None:
        if not self.started:   # tui_claude.start leaves no session when it raises; a live one of that name isn't this agent run's
            return
        try:
            tui_claude.kill(self.name)
        except tui_claude.TuiError as e:   # the driver's own exception is the one to raise
            print(f"drive.py: tui: {e}", file=sys.stderr)


RUNNERS: dict[str, type[Runner]] = {"headless": Headless, "tui": Tui}


def command(launch: Launch, runner: str, client: Client) -> list[str]:
    """The command `runner` starts; raises ConfigError for an unknown runner or a client without that command."""
    if runner not in RUNNERS:
        raise ConfigError(f"unknown runner {runner!r} (known: {', '.join(RUNNERS)})")
    if argv := getattr(launch, RUNNERS[runner].starts):
        return argv
    raise ConfigError(f"{type(client).__name__} has no {runner} command")


def check_layout(runner: str, layout: Layout | None) -> None:
    """Raises ConfigError for a layout the runner can't take."""
    if layout is None:
        return
    if runner == "headless":
        raise ConfigError("the headless runner takes no layout")
    if layout.split is not None and layout.split not in tui_claude.SPLITS:
        raise ConfigError(f"layout split {layout.split!r}: want one of {', '.join(tui_claude.SPLITS)}")
    if layout.beside is not None and not tui_claude.NAME.fullmatch(layout.beside):
        raise ConfigError(f"layout beside {layout.beside!r}: want {tui_claude.NAME.pattern}")
    if layout.opener is not None and not tui_claude.OPENER.fullmatch(layout.opener):
        raise ConfigError(f"layout opener {layout.opener!r}: want a tmux session name or an iTerm2 session id")


def check_naming(runner: str, prefix: str | None, events: str | None) -> None:
    """Raises ConfigError for a session prefix or an events file the runner can't take."""
    if runner == "headless":
        for what, value in (("prefix", prefix), ("events", events)):
            if value is not None:
                raise ConfigError(f"the headless runner takes no {what}")
    if prefix is not None and not tui_claude.NAME.fullmatch(prefix):
        raise ConfigError(f"prefix {prefix!r}: want {tui_claude.NAME.pattern}")


def start(launch: Launch, run: RunConfig, params: RunParams, *, client: Client, runner: str = "headless",
          layout: Layout | None = None, prefix: str | None = None, events: str | None = None,
          sinks: Sequence[Sink] | None = None, begun: Callable[[], None] | None = None,
          popen=subprocess.Popen) -> Result:
    """Starts the agent run through `runner` (RUNNERS) and waits, handing `sinks` (the terminal when None) its host's
    text and the progress it reports to the channel as they come; then checks the last outcome it reported, saves the
    deliverable to params.out where the destination says so, and hands the outcome on too. Only reports made after
    this call began count. The Record holds the session, its progress and the checked outcome; `begun` is called once
    its first write is done, before the host starts. A done or failed new run whose task marks `start` but never reported it gets a stderr line
    and a `missing` event first. The tui runner names its session `prefix` (default <role>-<task>) and sid, and
    appends its state events to the `events` file. Raises ConfigError, before anything starts, when the client lacks
    the runner's command or the layout, prefix or events is one the runner can't take (check_layout, check_naming);
    a RunnerError stops the runner and is the Result, with rc 1 and `<runner>: <reason>`."""
    argv = command(launch, runner, client)
    check_layout(runner, layout)
    check_naming(runner, prefix, events)
    host = RUNNERS[runner](run=run, params=params, client=client, popen=popen, layout=layout, prefix=prefix,
                           events=events, status_line=launch.status_line)
    workdir = os.path.abspath(params.workdir)
    os.makedirs(workdir, exist_ok=True)
    sinks = [terminal()] if sinks is None else sinks
    out = Path(params.out).absolute()
    if not out.is_dir():
        out.unlink(missing_ok=True)   # an earlier deliverable is never read as this agent run's
    append_line(params.channel, "")   # created before launch, so the agent run's report command finds it
    rec = Record(launch, params)
    try:
        if begun:
            begun()
        return _drive(launch, run, params, host=host, argv=argv, runner=runner, sinks=sinks, rec=rec, out=out)
    finally:
        rec.end()


def _drive(launch: Launch, run: RunConfig, params: RunParams, *, host: Runner, argv: list[str], runner: str,
           sinks: Sequence[Sink], rec: Record, out: Path) -> Result:
    """start()'s loop, from the host's begin to the checked outcome."""
    workdir = os.path.abspath(params.workdir)
    tail, raw, seen = Tail(params.channel), None, set()

    def hand(event: Event) -> None:
        nonlocal raw
        if event.kind == "outcome":
            raw = event.outcome
            return
        if event.kind == "stop":   # only an interactive agent run's turn end; no sink takes it
            return
        if event.kind == "progress":
            seen.add(event.name)
            rec.progress(event)
        for sink in sinks:
            sink(event)

    env = {k: v for k, v in os.environ.items() if k != tui_claude.CHILD_SESSION}
    try:
        host.begin(argv, cwd=launch.cwd or workdir, env={**env, **launch.env})
        while True:
            events, ended = host.poll(POLL)
            for event in tail():   # first: a report made before a stdout line comes before it
                host.seen(event)
                hand(event)
            for event in events:
                hand(event)
            if ended or host.done(raw is not None):
                break
    except RunnerError as e:
        host.stop()
        return Result(1, None, f"{runner}: {e}")
    except BaseException:
        host.stop()
        raise
    rc = host.returncode()
    for event in tail():
        hand(event)
    if rc != 0:
        return Result(rc, None, f"the client exited {rc}")
    if raw is None:
        return Result(rc, None, "the agent run returned no outcome")
    try:
        outcome = validate(raw, run, params)
    except InvalidOutcome as e:
        return Result(rc, None, f"invalid outcome: {e}")
    if run.output["type"] in SAVES_DELIVERABLE and outcome.deliverable:
        save(out, outcome.deliverable)
        if run.output["type"] == "local":
            outcome = replace(outcome, url=str(out))
    # A resumed session reported its start before the interruption.
    if outcome.status != "needs_input" and "start" in run.progress and "start" not in seen and not params.resume:
        print("drive.py: missing progress mark: start", file=sys.stderr)
        for sink in sinks:
            sink(Event("missing", name="start"))
    rec.outcome(asdict(outcome))
    for sink in sinks:
        sink(Event("outcome", outcome=asdict(outcome)))
    return Result(rc, outcome)


def main(argv: list[str], root: str = ROOT, popen=subprocess.Popen) -> int:
    ap = argparse.ArgumentParser(prog="drive.py")
    ap.add_argument("--role", required=True)
    ap.add_argument("--task")
    ap.add_argument("--input")
    ap.add_argument("--out")
    ap.add_argument("--workdir")
    ap.add_argument("--repo")
    # Given once: the act-as skill pre-approves `--client skill --role *`, which a second --client must not turn into
    # an agent run.
    ap.add_argument("--client", action=repos.Once, help="default: claude")
    ap.add_argument("--sid")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--runner", choices=RUNNERS, default="headless")
    ap.add_argument("--split", choices=tui_claude.SPLITS,
                    help="the tui runner's split (default: stacked with the panes of the same opener)")
    ap.add_argument("--beside", metavar="SESSION", help="split the pane showing this tmux session")
    ap.add_argument("--prefix", help="the tui session's name before the sid (default: <role>-<task>)")
    ap.add_argument("--events", metavar="FILE", help="the tui runner appends the session's state events to FILE")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.input == "-":
        a.input = sys.stdin.read()
    params = run = None
    layout = Layout(a.split, a.beside) if a.split or a.beside is not None else None
    name = a.client or "claude"
    try:
        check_naming(a.runner, a.prefix, a.events)
        client = clients.get(name, root)
        if client.runs:
            if a.input is None or a.out is None or a.workdir is None:
                raise ConfigError(f"client {name!r} needs --input, --out and --workdir")
            params = RunParams(input=a.input, out=a.out, workdir=a.workdir, sid=a.sid, resume=a.resume)
            launch, run = plan(root, client, a.role, a.task, params=params, repo=a.repo)
            cmd = command(launch, a.runner, client)
            check_layout(a.runner, layout)
        else:
            if a.runner != "headless":
                raise ConfigError(f"{type(client).__name__} prints a prompt; it takes no --runner {a.runner}")
            if layout:
                raise ConfigError(f"{type(client).__name__} prints a prompt; it takes no layout")
            prompt = inline(root, client, a.role, a.task)
    except ConfigError as e:
        print(f"drive.py: {e}", file=sys.stderr)
        return 2
    if not params:
        print(prompt, end="")
        return 0
    print(f"drive.py: session {params.sid}", file=sys.stderr)
    if a.dry_run:
        print(json.dumps({"argv": cmd, "cwd": launch.cwd, "env": launch.env}, ensure_ascii=False, indent=1))
        return 0
    result = start(launch, run, params, client=client, runner=a.runner, layout=layout, prefix=a.prefix,
                   events=a.events, popen=popen)
    if result.outcome is None:
        print(f"drive.py: {result.error}", file=sys.stderr)
        return 3 if result.returncode != 0 else 1
    print(f"drive.py: status {result.outcome.status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
