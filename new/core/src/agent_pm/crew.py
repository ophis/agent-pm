"""A manager's workers, each a plain tui session reached only through driver.api, and its roster (roster.py). start
picks the runtime (--client, else the caller's own tui session's, else the core config's `runtime`), starts the worker
with its workdir ~/.agent-pm/workers/<sid>/, records it there in worker.json (what restart replays), shows it, enters
it in the roster and watches it EARLY s for a death or a dialog. attach syncs the roster with the live sessions,
reopens the unshown and prints the table, the backlog and how to arm a monitor. Exits 1 on an error, 2 on a usage
error. Follow-ups go through the Driver's tmux_host.py send. Run as `env PYTHONPATH=<absolute IMPORT_ROOT> python3
-P -m agent_pm.crew …`, the form of each crew command it prints or runs (a worker entry's recovery); main first drops
each PYTHONPATH entry resolving to IMPORT_ROOT, so workers and recovered role or pipeline entries get the user's
own."""

import argparse
import contextlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator

from agent_pm import roster
from agent_pm.driver import api
from agent_pm.utils.files import save
from agent_pm.utils.paths import IMPORT_ROOT, printable_abs

NAME = re.compile(r"[A-Za-z0-9_-]+")
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")
EARLY = 5
REPORT_LINES = 20
REPLY_LINES = 80
PANE_LINES = 40
RUN_TAIL = 1 << 20
ENDED = {"done": "finished", "failed": "finished", "needs_input": "waiting"}
HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tpane"
ROTATE_MSG = "rotate: run crew.py attach, then arm with its N and GEN"
UNPRINTABLE = "want printable text (no control character, U+2028 or U+2029)"
RECORD = "worker.json"
RECORD_MAX = 64 << 10
FIELDS = (
    "version",
    "name",
    "sid",
    "runtime",
    "cwd",
    "flags",
    "env",
    "events",
    "manager",
    "split",
    "split_from",
)


class WorkersError(Exception):
    pass


class NotAWorker(WorkersError):
    pass


class Refused(WorkersError):
    pass


@contextlib.contextmanager
def _driver() -> Iterator[None]:
    try:
        yield
    except (api.ConfigError, api.DriverError) as e:
        raise WorkersError(str(e)) from e


def _check(name: str) -> None:
    if not NAME.fullmatch(name):
        raise WorkersError(f"bad name {name!r}: use [A-Za-z0-9_-]+")


def _rows(match: str | None = None) -> list[api.SessionRow]:
    with _driver():
        return api.sessions(match)


def _row(name: str) -> api.SessionRow | None:
    return next(iter(_rows(re.escape(name))), None)


def _live() -> dict[str, api.SessionRow]:
    return {row.name: row for row in _rows()}


def _caller() -> api.SessionRow | None:
    return next((row for row in _rows() if row.own), None)


def _own() -> str | None:
    row = _caller()
    return None if row is None else row.name


def _text(value) -> bool:
    return isinstance(value, str) and value != "" and roster.printable(value)


def _override(item: str) -> tuple[str, str]:
    key, eq, value = item.partition("=")
    if not (eq and key):
        raise WorkersError(f"--env {item}: want NAME=VALUE")
    if not roster.printable(value):
        raise WorkersError(f"--env {key}: {value!r}: {UNPRINTABLE}")
    return key, value


def _note_text(text: str) -> str | None:
    if len(text) > roster.NOTE_MAX:
        raise WorkersError(f"note: {len(text)} characters: want at most {roster.NOTE_MAX}")
    if not roster.printable(text):
        raise WorkersError(f"note: {text!r}: {UNPRINTABLE}")
    return text or None


def _runtime(client: str | None, own_runtime: str | None) -> str:
    if client is not None:
        return client
    if own_runtime is not None:
        return own_runtime
    try:
        return roster.default_runtime()
    except roster.RosterError as e:
        raise WorkersError(str(e)) from e


def _worker_dir(sid: str) -> str:
    try:
        return roster.worker_dir(sid)
    except roster.RosterError as e:
        raise WorkersError(str(e)) from e


def _spec(record: dict, workdir: str) -> api.Spec:
    protect = (roster.ROOT, roster.WORKERS, roster.CREW)
    with _driver():
        return api.Spec(
            workdir=workdir,
            access=api.Access(cwd=record["cwd"]),
            flags=tuple(record["flags"]),
            env=record["env"],
            protect=tuple(os.path.realpath(os.path.expanduser(p)) for p in protect),
            role_policy=False,
        )


def _enroll(record: dict, own: str | None, note: str | None) -> None:
    name, directory, split, split_from = (
        record[k] for k in ("name", "manager", "split", "split_from")
    )
    resume = [
        sys.executable,
        *roster.CREW_MODULE,
        "start",
        name,
        "--manager",
        os.path.basename(directory),
        "--client",
        record["runtime"],
        "--resume",
        record["sid"],
        "--cwd",
        record["cwd"],
        *(w for k, v in record["env"].items() for w in ("--env", f"{k}={v}")),
        *(["--split", split] if split is not None else []),
        *(["--split-from", split_from] if split_from is not None else []),
        *(["--", *record["flags"]] if record["flags"] else []),
    ]
    try:
        row = _row(name)
    except WorkersError:
        row = None
    try:
        with _leased(directory, own) as r:
            value = roster.entry(
                "worker",
                sid=record["sid"],
                cwd=record["cwd"],
                resume=resume,
                note=note,
                split=split,
                split_from=split_from,
            )
            roster.record(r, name, value if row is None else _placed(value, row))
    except WorkersError as e:
        roster.unwritten(name, e)


def _died(name: str, runtime: str, code: int) -> str:
    row = _row(name)
    if row is None:
        return f"{name}: session ended at once"
    lines = [x for x in (row.last_lines or "").splitlines() if x.strip()]
    return f"{name}: {runtime} exited {code} at once; its pane's last lines:\n" + "\n".join(
        lines[-REPORT_LINES:]
    )


def _early(name: str, runtime: str, handle: api.Handle) -> None:
    deadline = time.monotonic() + EARLY
    with _driver():
        for event in handle.events():
            if isinstance(event, api.Exited):
                raise WorkersError(_died(name, runtime, event.returncode))
            if isinstance(event, api.Blocked):
                print(f"workers: {name}: waits on a dialog: {event.reason}", file=sys.stderr)
                return
            if isinstance(event, api.IdleTimeout) or time.monotonic() >= deadline:
                return


def _launch(
    record: dict, spec: api.Spec, own: str | None, *, prompt: str, resume: str | None, note
) -> None:
    name = record["name"]
    with _driver():
        handle = api.start(
            record["runtime"],
            "tui",
            prompt,
            spec,
            resume=resume,
            idle_timeout=EARLY,
            name=name,
            sid=None if resume else record["sid"],
            events=record["events"],
        )
    path = os.path.join(spec.workdir, RECORD)
    try:
        save(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        with contextlib.suppress(api.DriverError):
            api.close(name)
        raise WorkersError(f"{path}: {e.strerror or e}; start undone") from e
    print(f"tui: session {name}: {api.attach_command(name)}", file=sys.stderr)
    with _driver():
        api.show(name, split=record["split"], split_from=record["split_from"])
    _enroll(record, own, note)
    _early(name, record["runtime"], handle)


def start(
    name: str,
    directory: str,
    *,
    own: str | None,
    cwd: str,
    own_runtime: str | None = None,
    prompt: str | None = None,
    note: str | None = None,
    flags=(),
    client: str | None = None,
    overrides=(),
    resume: str | None = None,
    split_from: str | None = None,
    split: str | None = None,
) -> str:
    _check(name)
    if split_from is not None:
        _check(split_from)
    if resume is not None and not roster.SID.fullmatch(resume):
        raise WorkersError(f"--resume {resume}: not a session id")
    if note is not None:
        note = _note_text(note)
    env = dict(_override(item) for item in overrides)
    with _leased(directory, own, write=False):
        pass
    cwd = os.path.abspath(cwd)
    if not os.path.isdir(cwd):
        raise WorkersError(f"--cwd {cwd}: not a directory")
    record = {
        "version": 1,
        "name": name,
        "sid": resume or str(uuid.uuid4()),
        "runtime": _runtime(client, own_runtime),
        "cwd": cwd,
        "flags": list(flags),
        "env": env,
        "events": roster.events(directory, create=False),
        "manager": os.path.abspath(directory),
        "split": split,
        "split_from": split_from,
    }
    workdir = _worker_dir(record["sid"])
    _launch(record, _spec(record, workdir), own, prompt=prompt or "", resume=resume, note=note)
    return record["sid"]


def _worker(data, name: str, sid: str) -> bool:
    if not (isinstance(data, dict) and set(data) == set(FIELDS)):
        return False
    flags, env, manager, split_from = (data[k] for k in ("flags", "env", "manager", "split_from"))
    return (
        type(data["version"]) is int
        and data["version"] == 1
        and (data["name"], data["sid"]) == (name, sid)
        and _text(data["runtime"])
        and all(printable_abs(data[k]) for k in ("cwd", "events", "manager"))
        and NAME.fullmatch(os.path.basename(manager)) is not None
        and isinstance(flags, list)
        and all(isinstance(f, str) and "\0" not in f for f in flags)
        and isinstance(env, dict)
        and all(
            k and "=" not in k and isinstance(v, str) and roster.printable(v)
            for k, v in env.items()
        )
        and data["split"] in (None, *roster.SPLITS)
        and (
            split_from is None
            or isinstance(split_from, str)
            and NAME.fullmatch(split_from) is not None
        )
    )


def _recorded(workdir: str, name: str, sid: str) -> dict:
    """The worker record, checked as start checks its options; anything else: NotAWorker."""
    try:
        fd = os.open(os.path.join(workdir, RECORD), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise NotAWorker(f"{name}: not a worker") from None
    try:
        st = os.fstat(fd)
        raw = b""
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
            with os.fdopen(fd, "rb", closefd=False) as f:
                raw = f.read(RECORD_MAX + 1)
        data = json.loads(raw) if len(raw) <= RECORD_MAX else None
    except (OSError, ValueError, RecursionError):
        data = None
    finally:
        os.close(fd)
    if not _worker(data, name, sid):
        raise NotAWorker(f"{name}: not a worker")
    return data


def restart(name: str, *, own: str | None) -> None:
    _check(name)
    row = _row(name)
    if row is None or row.sid is None or not roster.SID.fullmatch(row.sid):
        raise NotAWorker(f"{name}: not a worker")
    workdir = _worker_dir(row.sid)
    record = _recorded(workdir, name, row.sid)
    spec = _spec(record, workdir)
    with _driver():
        api.close(name)
    _launch(record, spec, own, prompt="", resume=row.sid, note=None)


def reply(name: str) -> str | None:
    _check(name)
    with _driver():
        return api.attach(name).reply


def next_event(events: str, after: int | None) -> tuple[int, str]:
    offset = lines = 0
    ident = None
    skip = after
    while True:
        try:
            with open(events, "rb") as f:
                st = os.fstat(f.fileno())
                if ident is None:
                    ident = (st.st_dev, st.st_ino)
                elif ident != (st.st_dev, st.st_ino):
                    raise WorkersError(f"events file {events}: replaced")
                if st.st_size < offset:
                    raise WorkersError(f"events file {events}: shrank")
                f.seek(offset)
                data = f.read()
        except FileNotFoundError:
            data = b""
        except OSError as e:
            raise WorkersError(f"events file {events}: {e.strerror or e}") from e
        complete = data[: data.rfind(b"\n") + 1]
        offset += len(complete)
        batch = complete.split(b"\n")[:-1]
        if skip is None:
            skip = len(batch)
        for raw in batch:
            lines += 1
            text = raw.decode("utf-8", errors="replace")
            if lines > skip and EVENT.fullmatch(text):
                return lines, text
        time.sleep(0.5)


def content(event: str) -> str:
    name, kind = event.split(" ")[1:3]
    if kind not in ("done", "blocked", "dead"):
        return ""
    if kind == "done":
        try:
            text = api.attach(name).reply
        except (api.ConfigError, api.DriverError):
            text = None
        if text is not None:
            return "\n".join(text.split("\n")[:REPLY_LINES])
    row = _row(name)
    if row is None:
        raise WorkersError(f"{name}: no tmux session")
    if row.state == "dead":
        return "\n".join((row.last_lines or "").split("\n")[-PANE_LINES:])
    with _driver():
        return api.attach(name).screen(PANE_LINES)


def _status(cwd: str) -> str | None:
    try:
        fd = os.open(os.path.join(cwd, "run.jsonl"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            at = max(0, st.st_size - RUN_TAIL)
            f.seek(max(0, at - 1))
            # from the byte before the tail: its first piece is the line the tail cuts, or empty
            lines = f.read(RUN_TAIL + 1).split(b"\n")[1 if at else 0 :]
    except OSError:
        return None
    finally:
        os.close(fd)
    for raw in reversed(lines):
        try:
            line = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if isinstance(line, dict) and line.get("kind") == "result":
            outcome = line.get("outcome")
            status = outcome.get("status") if isinstance(outcome, dict) else None
            return status if isinstance(status, str) else None
    return None


def _placed(e: dict, row: api.SessionRow) -> dict:
    for key in ("pane", "opener"):
        if getattr(row, key) is not None:
            with contextlib.suppress(roster.RosterError):
                e = roster.entry(**{**e, key: getattr(row, key)})
    return e


def _place(r: dict, name: str, row: api.SessionRow) -> None:
    r["entries"][name] = _placed(r["entries"][name], row)


def _sessions(name: str, e: dict, live: dict) -> tuple[str | None, str | None]:
    pane, driver = (name, None) if e["kind"] == "worker" else (e["tui"], name)
    return (pane if pane in live else None), (driver if driver in live else None)


def _sync(r: dict, live: dict[str, api.SessionRow]) -> dict[str, str]:
    entries = r["entries"]
    for name in sorted(entries):
        sid = entries[name]["sid"]
        if entries[name]["kind"] == "worker" and sid is not None and name not in live:
            to = [n for n, row in live.items() if row.sid == sid]
            if len(to) == 1 and to[0] not in entries:
                entries[to[0]] = entries.pop(name)
    panes = {}
    for name in sorted(entries):
        e = entries[name]
        pane, driver = _sessions(name, e, live)
        if pane is None and driver is None:
            e["state"] = "gone" if e["kind"] == "worker" else ENDED.get(_status(e["cwd"]), "gone")
            continue
        e["state"] = live[pane].state if pane else "working"
        if pane:
            _place(r, name, live[pane])
            panes[name] = pane
    return panes


def _reopen(panes: dict, entries: dict, live: dict[str, api.SessionRow]) -> tuple[dict, list]:
    shown, opened = {}, []
    for name in sorted(panes):
        session, e = panes[name], entries[name]
        if live[session].shown is not None:
            shown[name] = live[session].shown or "a terminal"
            continue
        print(f"tui: session {session}: {api.attach_command(session)}", file=sys.stderr)
        with _driver():
            api.show(session, split=e["split"], split_from=e["split_from"])
        opened.append((name, e["kind"], e["sid"], session))
    return shown, opened


def _pythonpath(argv: list[str]) -> dict[str, str]:
    return {"PYTHONPATH": IMPORT_ROOT} if tuple(argv[1:4]) == roster.CREW_MODULE else {}


def _command(argv: list[str]) -> str:
    env = "".join(f"{k}={shlex.quote(v)} " for k, v in _pythonpath(argv).items())
    return (f"env {env}" if env else "") + shlex.join(argv)


def _recovery(e: dict) -> str:
    argv = roster.recovery(e)
    if argv is None:
        return "none (no sid)"
    return (
        f"cd {shlex.quote(e['cwd'])} && {_command(argv)}" if e["kind"] == "role" else _command(argv)
    )


def _table(entries: dict, shown: dict) -> str:
    lines = [HEADER]
    for name in sorted(entries):
        e = entries[name]
        cells = [name, *("-" if e[k] is None else e[k] for k in HEADER.split("\t")[1:])]
        lines.append("\t".join(cells + ([f"shown in {shown[name]}"] if name in shown else [])))
    lines += [
        f"resume {name}: {_recovery(entries[name])}"
        for name in sorted(entries)
        if entries[name]["state"] == "gone"
    ]
    return "".join(f"{line}\n" for line in lines)


def _backlog(events: str, cursor: int) -> list[str]:
    texts = (
        raw.decode("utf-8", errors="replace")
        for raw in roster.following(events, cursor).split(b"\n")[:-1]
    )
    return [f"{n} {text}" for n, text in enumerate(texts, cursor + 1) if EVENT.fullmatch(text)]


def attach(
    directory: str, own: str | None, *, after: int | None = None, gen: int | None = None
) -> tuple[str, dict]:
    try:
        events = roster.events(directory)
        with roster.locked(directory) as r:
            roster.take(r, directory, own)
            if own is None:
                print(
                    f"workers: not in tmux: no lease on {directory}; another manager may attach",
                    file=sys.stderr,
                )
            if after is not None:
                roster.advance(r, directory, after, gen)
            # before the rotation: a raise after its rename would drop the new cursor and gen
            live = _live()
            roster.rotate(r, directory)
            panes = _sync(r, live)
            backlog, cursor, gen = _backlog(events, r["cursor"]), r["cursor"], r["gen"]
        shown, opened = _reopen(panes, r["entries"], live)
        if opened:
            live = _live()
            with roster.locked(directory) as r:
                for name, kind, sid, session in opened:
                    e = r["entries"].get(name)
                    if e is not None and (e["kind"], e["sid"]) == (kind, sid) and session in live:
                        _place(r, name, live[session])
        table = _table(r["entries"], shown)
    except roster.RosterError as e:
        raise WorkersError(str(e)) from e
    me, name = (
        _command(["python3", *roster.CREW_MODULE]),
        shlex.quote(os.path.basename(os.path.dirname(events))),
    )
    arm = [
        f"N={cursor} GEN={gen}",
        f"monitor: tail -n +{cursor + 1} -F {shlex.quote(events)}",
        f"monitor expired: {me} attach --manager {name} --after LINE --gen {gen} --resume",
        f"next-event: {me} next-event --manager {name} --after {cursor} --gen {gen}",
    ]
    return table + "".join(f"{line}\n" for line in backlog + arm), r["entries"]


def recover(entries: dict) -> Iterator[tuple[bool, str]]:
    for name in sorted(entries):
        e = entries[name]
        if e["state"] != "gone":
            continue
        try:
            argv = roster.recovery(e)
        except roster.RosterError as err:
            raise WorkersError(str(err)) from err
        if argv is None:
            yield False, f"resumed {name}: not run: no sid"
            continue
        try:
            res = subprocess.run(
                argv,
                cwd=e["cwd"],
                env={**os.environ, **_pythonpath(argv)},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except OSError as err:
            yield False, f"resumed {name}: not run: {err.strerror or err}"
            continue
        # e.g. Popen's ValueError for an argv item (a NUL, a lone surrogate)
        except Exception as err:
            yield False, f"resumed {name}: not run: {type(err).__name__}: {err}"
            continue
        last = next(
            (line.strip() for line in reversed(res.stdout.splitlines()) if line.strip()), ""
        )
        yield (
            res.returncode == 0,
            f"resumed {name}: exit {res.returncode}" + (f": {last}" if last else ""),
        )


@contextlib.contextmanager
def _leased(directory: str, own: str | None, *, write: bool = True):
    try:
        with roster.locked(directory, write=write) as r:
            roster.check(r, directory, own)
            yield r
    except roster.RosterError as e:
        raise WorkersError(str(e)) from e


def release(directory: str, own: str | None) -> None:
    with _leased(directory, own) as r:
        r["holder"] = None


def note(directory: str, own: str | None, name: str, text: str) -> None:
    _check(name)
    with _leased(directory, own) as r:
        if name not in r["entries"]:
            raise WorkersError(f"{name} not in roster")
        r["entries"][name]["note"] = _note_text(text)


def stop(directory: str, own: str | None, name: str) -> None:
    _check(name)
    with _leased(directory, own) as r:
        roster.stop(r, name)


def opened(own: str, live: dict[str, api.SessionRow], entries=lambda session: {}) -> list[str]:
    depth, level, todo = {}, 0, {own}
    while todo:
        level += 1
        found = {n for n, row in live.items() if row.opener in todo}
        found.update(
            n
            for s in todo
            for k, e in entries(s).items()
            for n in (k, e["tui"])
            if n in live and live[n].opener in (None, s)
        )
        todo = found - set(depth) - {own}
        depth.update((n, level) for n in todo)
    return sorted(depth, key=lambda n: (-depth[n], n))


def _entries(directory: str) -> dict:
    if not os.path.isdir(directory):
        return {}
    try:
        with roster.locked(directory, write=False) as r:
            return r["entries"]
    except roster.RosterError as e:
        print(f"workers: stop-all: {e}", file=sys.stderr)
        return {}


def stop_all(own: str | None, *, dry_run: bool = False) -> tuple[list[str], list[str]]:
    if own is None:
        raise WorkersError("stop-all: not in tmux: run it in the manager's tmux session")
    home = roster.directory(own)
    if os.path.isdir(home):
        with _leased(home, own, write=False):
            pass
    read = {}

    def entries(session):
        if session not in read:
            read[session] = _entries(roster.directory(session))
        return read[session]

    chosen = opened(own, _live(), entries)
    pipelines = {k for es in read.values() for k, e in es.items() if e["kind"] == "pipeline"}
    verb, left = "would stop", []
    if not dry_run:
        verb = "stopped"
        for name in chosen:
            with contextlib.suppress(api.DriverError):
                api.close(name)
        after = _live()
        left = [n for n in chosen if n in after]
        gone = set(chosen) - set(left)
        for session in [s for s, es in read.items() if es]:
            d = roster.directory(session)
            try:
                with _leased(d, own) if session == own else roster.locked(d) as r:
                    for k in [
                        k
                        for k, e in r["entries"].items()
                        if k in gone or e["tui"] in gone and k not in after
                    ]:
                        roster.remove(r, k)
            except (roster.RosterError, WorkersError) as e:
                print(f"workers: stop-all: {e}", file=sys.stderr)
    lines = [
        f"{verb} {n}" + ("; a scheduler may resume it" if n in pipelines else "")
        for n in chosen
        if n not in left
    ]
    return lines, left


def forget(directory: str, own: str | None, name: str) -> None:
    _check(name)
    with _leased(directory, own) as r:
        pane, driver = _sessions(name, roster.remove(r, name), _live())
        if pane or driver:
            raise WorkersError(f"{name}: session {pane or driver} is live")


def _directory(name: str | None) -> str:
    try:
        directory = roster.directory(name)
    except roster.RosterError as e:
        raise WorkersError(str(e)) from e
    if directory is None:
        raise WorkersError("no manager directory: run inside tmux or give --manager <name>")
    return directory


def _record(directory: str, own: str | None, after: int | None, gen: int) -> tuple[str, int, bool]:
    with _leased(directory, own) as r:
        try:
            after = roster.advance(r, directory, after, gen)
        except roster.Stale as e:
            raise Refused(str(e)) from e
        return roster.events(directory, create=False), after, roster.due(r, directory)


def _after(value: str) -> int | None:
    if value == "end":
        return None
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise argparse.ArgumentTypeError(f"{value!r}: use a line number or end")


def _number(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise argparse.ArgumentTypeError(f"{value!r}: use a non-negative integer")


def _drop_import_root() -> None:
    entries = os.environ.get("PYTHONPATH")
    if entries is None:
        return
    root = os.path.realpath(IMPORT_ROOT)
    kept = [p for p in entries.split(os.pathsep) if os.path.realpath(p) != root]
    if kept:
        os.environ["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        del os.environ["PYTHONPATH"]


def main(argv=None) -> int:
    _drop_import_root()
    argv = sys.argv[1:] if argv is None else list(argv)
    flags = []
    if "--" in argv:
        i = argv.index("--")
        argv, flags = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(prog="crew.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("name")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p.add_argument("--cwd")
    p.add_argument("--prompt")
    p.add_argument("--resume", metavar="SID", help="a session id")
    p.add_argument(
        "--client",
        metavar="NAME",
        help="the worker's runtime (default: your tui session's, else the core config's runtime)",
    )
    p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="sets a runtime-declared default in the worker's env; repeatable",
    )
    p.add_argument("--note", help="its roster note, as crew.py note sets it")
    p.add_argument("--split-from")
    p.add_argument("--split", choices=("right", "below"))
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
    p = sub.add_parser("next-event")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p.add_argument("--after", type=_after, required=True)
    p.add_argument("--gen", type=_number, required=True)
    p = sub.add_parser("attach")
    p.add_argument(
        "--manager", help="manager directory to attach to (default: your tmux session's)"
    )
    p.add_argument(
        "--after",
        type=_number,
        metavar="LINE",
        help="the line of the last event handled; with --gen",
    )
    p.add_argument(
        "--gen", type=_number, metavar="G", help="the generation of that line; with --after"
    )
    p.add_argument(
        "--resume", action="store_true", help="then run each gone entry's recovery command"
    )
    p = sub.add_parser("release")
    p.add_argument("--manager", help="manager directory to release (default: your tmux session's)")
    p = sub.add_parser("note")
    p.add_argument("name")
    p.add_argument("text", help="'' clears it")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p = sub.add_parser("stop")
    p.add_argument("name", help="a roster entry, or its tui session")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p = sub.add_parser(
        "stop-all", help="stop every session your tmux session opened, and those they opened"
    )
    p.add_argument("--dry-run", action="store_true", help="print them, stop nothing")
    p = sub.add_parser("forget")
    p.add_argument("name")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    a = ap.parse_args(argv)
    if a.cmd == "attach" and (a.after is None) != (a.gen is None):
        ap.error("--after and --gen go together")
    try:
        if a.cmd == "attach":
            text, entries = attach(_directory(a.manager), _own(), after=a.after, gen=a.gen)
            print(text, end="", flush=True)
            if a.resume:
                failed = False
                for ok, line in recover(entries):
                    print(line, flush=True)
                    failed = failed or not ok
                if failed:
                    return 1
        elif a.cmd == "release":
            directory = _directory(a.manager)
            release(directory, _own())
            print(f"workers: released {directory}")
        elif a.cmd == "note":
            note(_directory(a.manager), _own(), a.name, a.text)
        elif a.cmd == "stop":
            stop(_directory(a.manager), _own(), a.name)
        elif a.cmd == "stop-all":
            lines, left = stop_all(_own(), dry_run=a.dry_run)
            for line in lines or ([] if left else ["workers: stop-all: nothing to stop"]):
                print(line)
            if left:
                raise WorkersError(
                    f"stop-all: still live after tmux kill-session: {', '.join(left)}"
                )
        elif a.cmd == "forget":
            forget(_directory(a.manager), _own(), a.name)
        elif a.cmd == "start":
            directory = _directory(a.manager)
            caller = _caller()
            sid = start(
                a.name,
                directory,
                own=None if caller is None else caller.name,
                own_runtime=None if caller is None else caller.runtime,
                cwd=a.cwd or os.getcwd(),
                prompt=a.prompt,
                note=a.note,
                flags=flags,
                client=a.client,
                overrides=a.env,
                resume=a.resume,
                split_from=a.split_from,
                split=a.split,
            )
            print(f"{a.name} {sid}")
        elif a.cmd == "restart":
            restart(a.name, own=_own())
        elif a.cmd == "reply":
            text = reply(a.name)
            if text:
                print(text)
        else:
            events, after, due = _record(_directory(a.manager), _own(), a.after, a.gen)
            if due:
                raise Refused(ROTATE_MSG)
            line, event = next_event(events, after)
            print(f"{line} {event}")
            try:
                text = content(event)
            except WorkersError as e:
                text = f"workers: {e}"  # stdout: background readers watch only it
            if text:
                print(text)
    except Refused as e:
        print(f"workers: {e}")
        return 1
    except WorkersError as e:
        print(f"workers: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
