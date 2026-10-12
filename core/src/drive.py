#!/usr/bin/env python3
import argparse
import contextlib
import errno
import json
import os
import posixpath
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, get_args
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clients  # noqa: E402
import compose  # noqa: E402
import repo as repos  # noqa: E402
import roster  # noqa: E402
import tmux_host  # noqa: E402
from clients import Access, Client, Event, Launch  # noqa: E402
from compose import (  # noqa: E402
    CHANNEL,
    CONFIG,
    ROOT,
    ConfigError,
    RunConfig,
    RunParams,
    Team,
    fill,
    outcome_schema,
    render,
    report_command,
    resolve,
    tui_session,
)

Status = Literal["done", "needs_input", "failed"]
STATUSES = get_args(Status)
SAVES_DELIVERABLE = ("local", "orchestrator")
NAME = re.compile(r"[\w-]+")
POLL = 0.5
WAIT_LIMIT = 2 * 60 * 60
STOP_LIMIT = 3
NUDGE = ("Finish your task, then report its outcome with the report command your instructions name. "
         "If you are waiting for background work, wait for it first.")
SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)   # end a detached driver (tmux kill-session: SIGHUP)
PR_PATH = re.compile(r"/[^/]+/[^/]+/(pull/\d+|compare/\S+|tree/\S+)")
LIST_PARAMS = RunParams(input="", out="/agent-pm-list/work/out.md", workdir="/agent-pm-list/work",
                        sid="00000000-0000-0000-0000-000000000000")


class InvalidOutcome(Exception):
    pass


class RunnerError(Exception):
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
    outcome: Outcome | None
    error: str = ""


def bind(entry: str, repo: str | None) -> str | None:
    if entry == "repo":
        return os.path.abspath(repo) if repo else None
    return os.path.abspath(os.path.expanduser(entry))


def access(run: RunConfig, params: RunParams, *, repo: str | None, scripts: str, methods: str, tasks: str,
           cwd: str | None = None, project: bool = False, interpreter: str = "python3") -> Access:
    workdir = os.path.abspath(params.workdir)
    cwd = cwd or workdir
    dirs = ([workdir] if os.path.realpath(cwd) != os.path.realpath(workdir) else []) + [tasks]
    for key, entries in (("read", run.read), ("write", run.write)):
        for entry in entries:
            p = bind(fill(entry, {"methods": methods}, key), repo)
            if p and p not in dirs:
                dirs.append(p)
    values = {"scripts": scripts, "workdir": workdir}
    commands = [fill(c, values, "commands") for c in run.commands] + [f"{report_command(scripts, params, interpreter)} *"]
    return Access(dirs, commands + ([run.gate] if run.gate else []), cwd=cwd, project=project)


def trusted_dirs(root: str) -> frozenset[str]:
    try:
        return repos.trusted_dirs(repos.read_config(os.path.join(root, CONFIG)))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def status_line(root: str) -> bool:
    try:
        return repos.status_line(repos.read_config(os.path.join(root, CONFIG)))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def workers_per_column(root: str) -> int | None:
    try:
        return repos.workers_per_column(repos.read_config(os.path.join(root, CONFIG)))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def grid_retile(root: str) -> str | None:
    try:
        return repos.grid_retile(repos.read_config(os.path.join(root, CONFIG)))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def place(root: str, run: RunConfig, params: RunParams, cwd: str | None = None,
          *, client: Client | None = None) -> tuple[str, bool]:
    workdir, trusted = os.path.abspath(params.workdir), trusted_dirs(root)
    if params.resume:
        location = client.resume_location(workdir, params.sid) if client is not None else None
        if location is not None:
            here, project = location
        else:
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


def check_team_dirs(dirs: Sequence[str], *, writes: Sequence[tuple[str, str]],
                    temp: Sequence[str] | None = None) -> None:
    if not dirs:
        return
    temps, me = repos.temp_dirs() if temp is None else temp, os.getuid()
    for d in map(os.path.realpath, dirs):
        for path in team_paths(d):
            real = os.path.realpath(path)
            shown = path if real == path else f"{path} ({real})"
            if t := repos.under(real, temps):
                raise ConfigError(f"team_dirs: {shown} is in temp dir {t}: any process can write there")
            check_overlap(shown, real, writes)
            try:
                st = os.stat(path)
            except OSError as e:
                raise ConfigError(f"{path}: {e.strerror}") from None
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ConfigError(f"team_dirs: {shown} is writable by group or others: chmod go-w it")
            if st.st_uid != me:
                raise ConfigError(f"team_dirs: {shown} is owned by uid {st.st_uid}, not you (uid {me}): chown it")
        for parent in Path(d).parents:
            try:
                st = os.stat(parent)
            except OSError as e:
                raise ConfigError(f"{parent}: {e.strerror}") from None
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not st.st_mode & stat.S_ISVTX:
                raise ConfigError(f"team_dirs: {d}'s parent dir {parent} is writable by group or others: they could "
                                  "replace the team dir")
            if st.st_uid not in (me, 0):
                raise ConfigError(f"team_dirs: {d}'s parent dir {parent} is owned by uid {st.st_uid}, not you or root: "
                                  "they could replace the team dir")


def team_paths(d: str) -> list[str]:
    paths = [d]
    for kind in compose.KINDS:
        if os.path.isdir(sub := os.path.join(d, kind)):
            try:
                names = sorted(os.listdir(sub))
            except OSError as e:
                raise ConfigError(f"{sub}: {e.strerror}") from None
            paths += [sub, *(os.path.join(sub, n) for n in names if n.endswith(".md"))]
    return paths


def check_overlap(shown: str, real: str, writes: Sequence[tuple[str, str]]) -> None:
    for what, p in writes:
        p = os.path.realpath(p)
        if repos.under(real, [p]) or repos.under(p, [real]):
            raise ConfigError(f"team_dirs: {shown} overlaps {what} {p}: the agent run can write there")


def plan(root: str, client: Client, role: str, task: str | None = None, *, params: RunParams,
         repo: str | None = None, layers: Sequence[Mapping] = (), cwd: str | None = None, team: Team | None = None,
         temp: Sequence[str] | None = None) -> tuple[Launch, RunConfig]:
    if not client.runs:
        raise ConfigError(f"{type(client).__name__} prints a prompt; use inline()")
    guarded: tuple[str, ...] = ()
    if team is None:
        team = compose.team(root)
        try:
            guarded = compose.team_dirs(root)
        except (ConfigError, OSError, ValueError):
            pass
    run = resolve(root, client, role, task, overlays=layers, trusted=True, team=team)
    here, project = place(root, run, params, cwd, client=client)
    workdir, methods = os.path.abspath(params.workdir), client.methods_path(root)
    own = team.tasks_dir(role)
    tasks = own or client.tasks_path(root)
    prompt = render(root, run, params, client=client, team=team, tasks=tasks)
    acc = access(run, params, repo=repo, scripts=client.scripts_path(root), methods=methods, tasks=tasks, cwd=here,
                 project=project, interpreter=client.report_interpreter())
    launch = client.launch(prompt, run, params=params, access=acc)
    if team.dirs or guarded:
        writes = [("the workdir", workdir), ("the cwd", here)]
        writes += [(f"write dir {e}", p) for e in run.write if (p := bind(fill(e, {"methods": methods}, "write"), repo))]
        name = type(client).__name__
        added = [(f"a dir {name} adds", p) for p in acc.dirs] + [(f"a dir {name} can write", p)
                                                                  for p in launch.writable_roots]
        # All but the role's own tasks dir, which the client adds for the run to read.
        writes += [(what, p) for what, p in added if not own or os.path.realpath(p) != os.path.realpath(own)]
        check_team_dirs(team.dirs, writes=writes, temp=temp)
        for path in (p for d in guarded for p in team_paths(d)):
            real = os.path.realpath(path)
            check_overlap(path if real == path else f"{path} ({real})", real, writes)
    return replace(launch, project=project, status_line=status_line(root), per_column=workers_per_column(root),
                   retile=grid_retile(root)), run


def inline(root: str, client: Client, role: str, task: str | None = None, *, team: Team | None = None,
           temp: Sequence[str] | None = None) -> str:
    if client.runs:
        raise ConfigError(f"{type(client).__name__} starts agent runs; use plan()")
    team = team if team is not None else compose.team(root)
    check_team_dirs(team.dirs, writes=[], temp=temp)
    return client.inline(render(root, resolve(root, client, role, task, team=team), client=client, team=team))


def listing(root: str, client: Client, team: Team, temp: Sequence[str] | None = None) -> str:
    check_team_dirs(team.dirs, writes=[], temp=temp)
    team.check_all(repos.read_config(os.path.join(root, CONFIG)))
    params, lines = LIST_PARAMS if client.runs else None, []
    for role, tasks in compose.catalog(root, team).items():
        run = resolve(root, client, role, team=team)
        render(root, run, params, client=client, team=team)
        lines.append(f"{role}  {run.output['type']}  {team.source(team.lookup('roles', role))}\n")
        lines += [f"  {t}: {what}  [{team.source(team.lookup('tasks', t))}]\n" for t, what in tasks.items()]
    return "".join(lines)


# The agent run controls its workdir, where the driver writes too: writes replace a symlink planted at the path, never
# follow it.
def save(path: str | Path, text: str) -> None:
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


def holds(path: str | Path, text: str) -> bool:
    data = text.encode()
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
            return stat.S_ISREG(os.fstat(f.fileno()).st_mode) and f.read(len(data) + 1) == data
    except OSError:
        return False


def append_line(path: str | Path, text: str, *, durable: bool = False) -> None:
    data = memoryview(text.encode(errors="replace"))
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        while data:
            data = data[os.write(fd, data):]
        if durable:
            os.fsync(fd)
    finally:
        os.close(fd)
    if durable:
        parent = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def printable(text: str) -> str:
    # control characters but tabs dropped, so an agent run's output can't steer the terminal
    return "".join(c for c in text if c.isprintable() or c == "\t")


def validate(data: dict, run: RunConfig, params: RunParams) -> Outcome:
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
    base = Path(workdir).resolve()
    real = (base / path).resolve() if isinstance(path, str) else None
    # A hard link is the same file under another name: it could carry, e.g., a key into the workdir.
    if (real is None or real.suffix != ".md" or not real.is_relative_to(base) or not real.is_file()
            or real.stat().st_nlink != 1):
        raise InvalidOutcome(f"file {str(path)[:200]!r} is not a single-link .md file under the workdir")
    return str(real)

Sink = Callable[[Event], None]


def terminal(log=sys.stderr) -> Sink:
    def sink(event: Event) -> None:
        if event.kind in ("text", "stderr"):
            print(printable(event.text), file=log, flush=True)
        elif event.kind == "progress":
            print(printable(f"Progress ({event.name}): {event.text}"), file=log, flush=True)
    return sink


def stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def note(path: str, kind: str, *, best_effort: bool = False, durable: bool = False, **fields) -> None:
    try:
        line = json.dumps({"ts": stamp(), "kind": kind, **fields}, ensure_ascii=False) + "\n"
        if durable:
            append_line(path, line, durable=True)
        else:
            append_line(path, line)
    except OSError as e:
        if not best_effort:
            raise
        print(f"drive.py: {e}", file=sys.stderr)


def session(workdir: str, sid: str) -> dict | None:
    try:
        fd = os.open(os.path.join(workdir, CHANNEL), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        lines = f.read().split(b"\n")
    found = None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if (isinstance(e, dict) and e.get("kind") == "session" and e.get("sid") == sid and isinstance(e.get("cwd"), str)
                and os.path.isabs(e["cwd"]) and e["cwd"].isprintable() and isinstance(e.get("project"), bool)):
            found = e
    return found


def session_info(client: Client, workdir: str, sid: str):
    return client.session_info(workdir, sid)


def report_event(line: str) -> Event | None:
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
            if os.fstat(f.fileno()).st_size < self.pos:
                self.pos, self.rest = 0, b""
            f.seek(self.pos)
            data = f.read()
        self.pos += len(data)
        *lines, self.rest = (self.rest + data).split(b"\n")
        for line in lines:
            if event := report_event(line.decode("utf-8", "replace")):
                yield event


TUI_SESSION = re.compile(r"(?P<prefix>[A-Za-z0-9_-]+)-[0-9a-f]{8}")


def driver_session(role: str, sid: str, prefix: str | None = None) -> str:
    return f"{tui_session(role, sid, prefix)}-drive"


@dataclass(frozen=True)
class Layout:
    split: str | None = None
    split_from: str | None = None
    opener: str | None = None


class Runner(Protocol):
    starts: str

    # begin and poll raise RunnerError when the host fails.
    def begin(self, argv: list[str], *, cwd: str, env: dict[str, str]) -> None: ...
    def poll(self, timeout: float) -> tuple[list[Event], bool]: ...
    def seen(self, event: Event) -> None: ...
    def done(self, outcome_arrived: bool) -> bool: ...
    def returncode(self) -> int: ...
    def stop(self) -> None: ...   # on a driver exception or a RunnerError, begin's too


class Headless:
    starts = "argv"

    def __init__(self, *, run: RunConfig, params: RunParams, client: Client, popen, layout: Layout | None,
                 events: str | None = None, status_line: bool = False, per_column: int | None = None,
                 retile: str | None = None):
        self.client, self.popen, self.proc = client, popen, None

    def begin(self, argv: list[str], *, cwd: str, env: dict[str, str]) -> None:
        self.proc = self.popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, errors="replace")
        self.items, self.open = queue.Queue(), 2
        for events in (self.client.events(self.proc.stdout),
                       (Event("stderr", line.rstrip("\n")) for line in self.proc.stderr)):
            threading.Thread(target=self.pump, args=(events,), daemon=True).start()

    def pump(self, events: Iterator[Event]) -> None:
        try:
            for event in events:
                self.items.put(event)
        except BaseException as e:
            self.items.put(e)
        else:
            self.items.put(None)

    def poll(self, timeout: float) -> tuple[list[Event], bool]:
        try:
            item = self.items.get(timeout=timeout)
        except queue.Empty:
            return [], False
        if isinstance(item, BaseException):
            raise item
        if item is None:
            self.open -= 1
            return [], self.open == 0
        return [item], False

    def seen(self, event: Event) -> None:
        pass

    def done(self, outcome_arrived: bool) -> bool:
        return False

    def returncode(self) -> int:
        return self.proc.wait()

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()


class Tui:
    starts = "interactive"

    def __init__(self, *, run: RunConfig, params: RunParams, client: Client, popen, layout: Layout | None,
                 events: str | None = None, status_line: bool = False, per_column: int | None = None,
                 retile: str | None = None):
        self.run, self.layout, self.events, self.status_line = run, layout or Layout(), events, status_line
        self.per_column, self.retile = per_column, retile
        self.name = tui_session(run.role, params.sid, params.prefix)
        self.rc, self.outcome, self.nudged, self.stops, self.gave_up = 0, False, False, 0, False
        self.started, self.since = False, 0.0

    def begin(self, argv: list[str], *, cwd: str, env: dict[str, str]) -> None:
        try:
            tmux_host.start(self.name, argv, cwd=cwd, env=env, events=self.events, template=self.run.show,
                             split=self.layout.split, split_from=self.layout.split_from, opener=self.layout.opener,
                             per_column=self.per_column, retile=self.retile, status_line=self.status_line)
        except tmux_host.TuiError as e:
            raise RunnerError(str(e)) from e
        self.started, self.since = True, time.monotonic()

    def poll(self, timeout: float) -> tuple[list[Event], bool]:
        time.sleep(timeout)
        try:
            state = tmux_host.status(self.name)
        except tmux_host.TuiError as e:
            raise RunnerError(str(e)) from e
        if state == tmux_host.RUNNING:
            return [], False
        self.rc = state or 0
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
                    tmux_host.send(self.name, NUDGE)
                except tmux_host.TuiError as e:
                    print(f"drive.py: tui: {e}", file=sys.stderr)
                return
            self.stops += 1
            if self.stops >= STOP_LIMIT:
                self.give_up(f"no outcome after {self.stops} stops")

    def give_up(self, reason: str) -> None:
        self.gave_up = True
        print(f"drive.py: {reason}; session {self.name} left open: {tmux_host.attach_command(self.name)}", file=sys.stderr)

    def done(self, outcome_arrived: bool) -> bool:
        if outcome_arrived:
            return True
        if not self.gave_up and time.monotonic() - self.since > WAIT_LIMIT:
            self.give_up(f"no outcome {WAIT_LIMIT / 3600:g} h after the last progress report")
        return self.gave_up

    def returncode(self) -> int:
        return self.rc

    def stop(self) -> None:
        if not self.started:   # tmux_host.start leaves no session when it raises; a live one of that name isn't this agent run's
            return
        try:
            tmux_host.kill(self.name)
        except tmux_host.TuiError as e:   # the driver's own exception is the one to raise
            print(f"drive.py: tui: {e}", file=sys.stderr)


RUNNERS: dict[str, type[Runner]] = {"headless": Headless, "tui": Tui}


def command(launch: Launch, runner: str, client: Client) -> list[str]:
    if runner not in RUNNERS:
        raise ConfigError(f"unknown runner {runner!r} (known: {', '.join(RUNNERS)})")
    if argv := getattr(launch, RUNNERS[runner].starts):
        return argv
    raise ConfigError(f"{type(client).__name__} has no {runner} command")


def check_layout(runner: str, layout: Layout | None) -> None:
    if layout is None:
        return
    if runner == "headless":
        raise ConfigError("the headless runner takes no layout")
    if layout.split is not None and layout.split not in tmux_host.SPLITS:
        raise ConfigError(f"layout split {layout.split!r}: want one of {', '.join(tmux_host.SPLITS)}")
    if layout.split_from is not None and not tmux_host.NAME.fullmatch(layout.split_from):
        raise ConfigError(f"layout split_from {layout.split_from!r}: want {tmux_host.NAME.pattern}")
    if layout.opener is not None and not tmux_host.OPENER.fullmatch(layout.opener):
        raise ConfigError(f"layout opener {layout.opener!r}: want a tmux session name or an iTerm2 session id")


def check_naming(runner: str, prefix: str | None, events: str | None) -> None:
    if runner == "headless":
        for what, value in (("prefix", prefix), ("events", events)):
            if value is not None:
                raise ConfigError(f"the headless runner takes no {what}")


def caller_layout(split: str | None, split_from: str | None, *, proc=subprocess.run) -> Layout:
    if split_from is not None:
        tmux_host.anchor(split_from, proc=proc)
    try:
        opener = tmux_host.opener(proc=proc)
    except tmux_host.TuiError:
        opener = None
    return Layout(split, split_from, opener)


def detach(name: str, argv: list[str], *, cwd: str, env: Mapping[str, str], iterm: str, proc=subprocess.run,
           sleep=time.sleep, roster_entry: tuple[str, dict] | None = None, opener: str | None = None) -> None:
    for arg in argv:   # execve would fail after the handover is taken
        if "\0" in arg:
            raise RunnerError("an argv item holds a NUL character")
        try:
            os.fsencode(arg)
        except UnicodeEncodeError:
            raise RunnerError("an argv item cannot be encoded") from None
    tmp, started = None, False
    try:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "handover.json")
        for arg in (sys.executable, path):
            if "#" in arg or arg.endswith(";"):
                raise RunnerError(f"tmux would misread {arg!r}")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump({"argv": argv, "cwd": cwd, "block": [int(s) for s in SIGNALS],
                       "env": {k: v for k, v in env.items() if k not in tmux_host.TERMINAL_KEYS}}, f)
        res = proc(["tmux", "new-session", "-d", "-e", f"ITERM_SESSION_ID={iterm}", "-s", name, sys.executable, "-I",
                    "-c", tmux_host.EXEC, path], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if res.returncode:
            raise RunnerError(f"tmux: {(res.stderr or '').strip()}")
        if opener is not None:
            res = proc(["tmux", "set-option", "-t", f"={name}:", "@opener", opener], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
            if res.returncode:
                print(f"drive.py: driver session {name}: @opener: {(res.stderr or '').strip()}", file=sys.stderr)
        for _ in range(round(tmux_host.HANDOVER_TIMEOUT / tmux_host.POLL)):
            if not os.path.exists(path):
                started = True
                break
            sleep(tmux_host.POLL)
    except OSError as e:
        raise RunnerError(f"driver session: {e}") from e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)   # first, so a late start finds no file to run
    if not started:
        with contextlib.suppress(tmux_host.TuiError):
            tmux_host.kill(name, proc=proc)
        raise RunnerError(f"session {name} did not start")
    if roster_entry is not None:
        directory, fields = roster_entry
        try:
            roster.put(directory, name, roster.entry(**fields))
        except roster.RosterError as e:
            roster.unwritten(name, e)


def start(launch: Launch, run: RunConfig, params: RunParams, *, client: Client, runner: str = "headless",
          layout: Layout | None = None, events: str | None = None,
          sinks: Sequence[Sink] | None = None, begun: Callable[[], None] | None = None,
          popen=subprocess.Popen) -> Result:
    argv = command(launch, runner, client)
    check_layout(runner, layout)
    check_naming(runner, params.prefix, events)
    host = RUNNERS[runner](run=run, params=params, client=client, popen=popen, layout=layout, events=events,
                           status_line=launch.status_line, per_column=launch.per_column, retile=launch.retile)
    workdir = os.path.abspath(params.workdir)
    os.makedirs(workdir, exist_ok=True)
    if launch.session_state is not None:
        client.prepare_session(launch, params)
    sinks = [terminal()] if sinks is None else sinks
    out = Path(params.out).absolute()
    if not out.is_dir() and not params.resume:   # a resumed session keeps the draft it wrote there
        out.unlink(missing_ok=True)   # an earlier deliverable is never read as this agent run's
    note(params.channel, "input", text=params.input)
    note(params.channel, "session", sid=params.sid, cwd=launch.cwd or workdir, project=launch.project,
         transcript=launch.transcript, resume=launch.resume,
         **({"snapshot": launch.session_state} if launch.session_state is not None else {}),
         durable=launch.session_state is not None)
    rc = 1
    try:
        if begun:
            begun()
        result = _drive(launch, run, params, client=client, host=host, argv=argv, runner=runner, sinks=sinks, out=out)
        rc, closing = result.returncode, {"error": result.error}
        if result.outcome is not None:
            closing = {"outcome": {k: v for k, v in asdict(result.outcome).items() if k != "deliverable"},
                       **({"error": result.error} if result.error else {})}
        return result
    except BaseException as e:
        text = " ".join(str(e).split())
        closing = {"error": f"stopped: {type(e).__name__}" + (f": {text}" if text else "")}
        if isinstance(e, SystemExit) and isinstance(e.code, int):
            rc = e.code
        raise
    finally:
        note(params.channel, "result", best_effort=True, **closing)
        note(params.channel, "end", best_effort=True, sid=params.sid, rc=rc)


def _drive(launch: Launch, run: RunConfig, params: RunParams, *, client: Client, host: Runner, argv: list[str], runner: str,
           sinks: Sequence[Sink], out: Path) -> Result:
    workdir = os.path.abspath(params.workdir)
    tail, raw, seen, session_seen = Tail(params.channel), None, set(), False
    pending: list[Event] = []

    def hand(event: Event) -> None:
        nonlocal raw, session_seen
        if event.kind == "session":
            if launch.session_state is None:
                raise ConfigError("client reported a native session without recovery state")
            client.record_session(params, event.session_id)
            note(params.channel, "client_session", sid=params.sid, client_id=event.session_id, durable=True)
            session_seen = True
            return
        if event.kind == "outcome":
            raw = event.outcome
            return
        if event.kind == "stop":   # only an interactive agent run's turn end; no sink takes it
            return
        if event.kind == "stderr":   # a diagnostic: a failed write loses the line, never the run
            note(params.channel, "stderr", best_effort=True, text=event.text)
        if event.kind == "progress":
            seen.add(event.name)
        for sink in sinks:
            sink(event)

    env = {k: v for k, v in os.environ.items() if k not in tmux_host.PARENT_KEYS}
    cwd = launch.cwd or workdir
    try:
        host.begin(argv, cwd=cwd, env={**env, **launch.env, "PWD": cwd})   # Linux claude takes its cwd from $PWD
        while True:
            events, ended = host.poll(POLL)
            # A headless client's first native ID must be durable before reports or later output are observed.
            for event in events:
                if event.kind == "session":
                    hand(event)
                else:
                    pending.append(event)
            if launch.session_state is None or session_seen:
                for event in tail():   # first: a report made before a stdout line comes before it
                    host.seen(event)
                    hand(event)
                for event in pending:
                    hand(event)
                pending.clear()
            if ended or host.done(raw is not None):
                break
    except RunnerError as e:
        host.stop()
        return Result(1, None, f"{runner}: {e}")
    except BaseException:
        host.stop()
        raise
    rc = host.returncode()
    if launch.session_state is not None and not session_seen:
        for event in pending:
            if event.kind == "stderr":
                hand(event)
        return Result(rc, None, "the client did not report a native session ID")
    for event in tail():
        hand(event)
    diagnostic = f"the client exited {rc}" if rc != 0 else ""
    if raw is None:
        return Result(rc, None, diagnostic or "the agent run returned no outcome")
    try:
        outcome = validate(raw, run, params)
    except InvalidOutcome as e:
        return Result(rc, None, diagnostic or f"invalid outcome: {e}")
    if run.output["type"] in SAVES_DELIVERABLE and outcome.deliverable:
        if not holds(out, outcome.deliverable):   # else the agent run wrote it there (params.deliverable)
            save(out, outcome.deliverable)
        if run.output["type"] == "local":
            outcome = replace(outcome, url=str(out))
    # A resumed session reported its start before the interruption.
    if outcome.status != "needs_input" and "start" not in seen and not params.resume:
        print("drive.py: missing progress mark: start", file=sys.stderr)
        for sink in sinks:
            sink(Event("missing", name="start"))
    for sink in sinks:
        sink(Event("outcome", outcome=asdict(outcome)))
    return Result(rc, outcome, diagnostic)


def main(argv: list[str], root: str = ROOT, popen=subprocess.Popen, proc=subprocess.run,
         temp: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="drive.py")
    ap.add_argument("--list", action="store_true",
                    help="list each role, its destination type and tasks, with their sources")
    ap.add_argument("--role")
    ap.add_argument("--task")
    ap.add_argument("--input", metavar="TEXT|-", help="the input text, even when it names a file; -: stdin's")
    ap.add_argument("--out")
    ap.add_argument("--workdir")
    ap.add_argument("--repo")
    # Given once: the act-as skill pre-approves `--client skill --role *`, which a second --client must not turn into
    # an agent run.
    ap.add_argument("--client", action=repos.Once, help="default: claude")
    ap.add_argument("--sid")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--runner", choices=RUNNERS, default="headless")
    ap.add_argument("--split", choices=tmux_host.SPLITS,
                    help="the tui runner's split, ignored in a tmux grid (default: stacked with the panes of the same "
                         "opener)")
    ap.add_argument("--split-from", metavar="SESSION",
                    help="split the pane showing this tmux session; ignored in a tmux grid")
    ap.add_argument("--prefix", help="the tui session's name before the sid (default: <role>)")
    ap.add_argument("--events", metavar="FILE", help="the tui runner appends the session's state events to FILE; with "
                                                     "--detach, the driver its outcome")
    ap.add_argument("--manager", metavar="NAME",
                    help="the manager directory whose events file the run uses when --events is not given (default: "
                         "the caller's tmux session's; core/skills/tmux/SKILL.md › Start 1)")
    ap.add_argument("--detach", action="store_true",
                    help="run the driver in a new detached tmux session, print its name and exit (needs --events or "
                         "--manager)")
    ap.add_argument("--opener", help=argparse.SUPPRESS)
    ap.add_argument("--driver", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.list and (extra := _list_extra(argv)):
        ap.error(f"--list takes only --client, not {extra}")
    if not a.list and a.role is None:
        ap.error("the following arguments are required: --role")
    a.running_client = False
    a.selected_client = None
    a.client_error = None
    if a.input == "-" and a.driver is None:   # the driver's is the caller's stdin text, even a "-"
        a.input = sys.stdin.read()
    if a.driver is None:
        return _main(a, root, popen, proc, temp)[0]
    status = None
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        for s in SIGNALS:
            signal.signal(s, _stop)
        try:
            module = clients.load_module(a.client or "claude")
            a.running_client = module.RUNS
            a.selected_client = clients.get(a.client or "claude", root, module=module)
        except ConfigError as e:
            a.client_error = e
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)   # blocked by detach until now
            code, status = _main(a, root, popen, proc, temp)
            return code
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    finally:   # a stop that cut the block short comes here too, blocked by _stop
        # Known inline clients never gain event-write authority, even when constructor validation fails.
        if a.running_client and a.events is not None:
            try:
                append_line(tmux_host.events_file(a.events),
                            f"{time.strftime('%H:%M:%S')} {a.driver} outcome {status or 'error'}\n")
            except (OSError, tmux_host.TuiError) as e:
                print(f"drive.py: {e}", file=sys.stderr)


def _list_extra(argv: Sequence[str]) -> str | None:
    args = iter(argv)
    for arg in args:
        name = arg.split("=", 1)[0]
        if len(name) >= 3 and "--client".startswith(name):
            if "=" not in arg:
                next(args, None)
        elif not (len(arg) >= 3 and "--list".startswith(arg)):
            return arg
    return None


def _stop(signum, frame):
    signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)   # one exit: the outcome line is written once
    raise SystemExit(128 + signum)


def _main(a: argparse.Namespace, root: str, popen, proc, temp=None) -> tuple[int, Status | None]:
    params = run = None
    layout = (Layout(a.split, a.split_from, a.opener) if a.split or a.split_from is not None or a.opener is not None
              else None)
    name = a.client or "claude"
    try:
        if a.manager is not None and a.runner != "tui" and not a.detach:
            raise ConfigError("--manager needs --runner tui or --detach")
        check_naming(a.runner, a.prefix, None if a.detach or a.driver is not None else a.events)
        if a.client_error is not None:
            raise a.client_error
        client = a.selected_client if a.selected_client is not None else clients.get(name, root)
        team = compose.team(root, compose.team_dirs(root))
        if a.list:
            print(listing(root, client, team, temp), end="")
            return 0, None
        if client.runs:
            if a.input is None or a.out is None or a.workdir is None:
                raise ConfigError(f"client {name!r} needs --input, --out and --workdir")
            params = RunParams(input=a.input, out=a.out, workdir=a.workdir, sid=a.sid, resume=a.resume, prefix=a.prefix)
            launch, run = plan(root, client, a.role, a.task, params=params, repo=a.repo, team=team, temp=temp)
            cmd = command(launch, a.runner, client)
            check_layout(a.runner, layout)
            driver = driver_session(run.role, params.sid, params.prefix)
            if a.detach and not tmux_host.NAME.fullmatch(driver):
                raise ConfigError(f"driver session {driver!r}: want {tmux_host.NAME.pattern}")
            if a.driver is None and a.events is None and (a.runner == "tui" or a.detach):
                try:
                    directory = roster.directory(a.manager, proc=proc)
                    if directory is not None:
                        a.events = roster.events(directory, create=not a.dry_run)
                except roster.RosterError as e:
                    raise ConfigError(str(e)) from e
            if a.detach and a.events is None:
                raise ConfigError("--detach needs --events or --manager")
        else:
            if a.runner != "headless":
                raise ConfigError(f"{type(client).__name__} prints a prompt; it takes no --runner {a.runner}")
            if layout:
                raise ConfigError(f"{type(client).__name__} prints a prompt; it takes no layout")
            if a.detach or a.driver is not None:
                raise ConfigError(f"{type(client).__name__} prints a prompt; it takes no --detach")
            prompt = inline(root, client, a.role, a.task, team=team, temp=temp)
    except ConfigError as e:
        print(f"drive.py: {e}", file=sys.stderr)
        return 2, None
    if not params:
        print(prompt, end="")
        return 0, None
    print(f"drive.py: session {params.sid}", file=sys.stderr)
    if a.dry_run:
        shown = {"argv": cmd, "cwd": launch.cwd, "env": launch.env, **({"driver": driver} if a.detach else {}),
                 **({"events": a.events} if a.events is not None else {})}
        print(json.dumps(shown, ensure_ascii=False, indent=1))
        return 0, None
    if a.detach:
        return _detach(a, run, params, driver, proc), None
    try:
        result = start(launch, run, params, client=client, runner=a.runner, layout=layout,
                       events=a.events if a.runner == "tui" else None, popen=popen)
    except ConfigError as e:
        print(f"drive.py: {e}", file=sys.stderr)
        return 2, None
    if result.outcome is None:
        print(f"drive.py: {result.error}", file=sys.stderr)
        return (3 if result.returncode != 0 else 1), None
    print(f"drive.py: status {result.outcome.status}", file=sys.stderr)
    return 0, result.outcome.status


def _detach(a: argparse.Namespace, run: RunConfig, params: RunParams, driver: str, proc) -> int:
    try:
        layout = caller_layout(a.split, a.split_from, proc=proc) if a.runner == "tui" else None
        events = tmux_host.events_file(a.events)
    except tmux_host.TuiError as e:
        print(f"drive.py: {e}", file=sys.stderr)
        return 2
    opener = layout and layout.opener
    given = {"role": a.role, "task": a.task, "input": a.input, "out": a.out, "workdir": a.workdir, "repo": a.repo,
             "client": a.client, "sid": params.sid, "runner": a.runner, "split": a.split, "split-from": a.split_from,
             "prefix": a.prefix, "events": events, "opener": opener, "driver": driver}
    argv = [sys.executable, os.path.abspath(__file__), *(f"--{k}={v}" for k, v in given.items() if v is not None),
            *(["--resume"] if a.resume else [])]
    # Without an opener the driver's own is its session, which no terminal shows: an empty $ITERM_SESSION_ID then
    # leaves it no pane, as the caller has none.
    iterm = os.environ.get("ITERM_SESSION_ID", "") if opener else ""
    try:
        d = roster.home(a.manager, events, proc=proc)
    except roster.RosterError as e:
        roster.unwritten(driver, e)
        d = None
    cwd = os.getcwd()
    tui = tui_session(run.role, params.sid, params.prefix) if a.runner == "tui" else None
    roster_entry = None if d is None else (d, {
        "kind": "role", "sid": params.sid, "cwd": cwd, "tui": tui, "opener": opener, "split": a.split,
        "split_from": a.split_from,
        "resume": [sys.executable, os.path.abspath(__file__),
                   *(f"--{k}={given[k]}" for k in ("role", "task", "out", "workdir", "repo", "client", "runner",
                                                   "split", "split-from", "prefix") if given[k] is not None),
                   "--detach", f"--manager={os.path.basename(d)}"]})
    try:
        detach(driver, argv, cwd=cwd, env=os.environ, iterm=iterm, proc=proc, roster_entry=roster_entry, opener=opener)
    except RunnerError as e:
        print(f"drive.py: {e}", file=sys.stderr)
        return 3
    print(f"drive.py: driver session {driver}: {tmux_host.attach_command(driver)}", file=sys.stderr)
    if tui is not None:
        print(f"drive.py: tui session {tui}: {tmux_host.attach_command(tui)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
