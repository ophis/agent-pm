from __future__ import annotations

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

CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(CORE, "src"))
import clients  # noqa: E402
import repo  # noqa: E402
import roster  # noqa: E402
import tmux_host  # noqa: E402
from compose import ConfigError  # noqa: E402

NAME = re.compile(r"[A-Za-z0-9_-]+")
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")
SESSION_ID = roster.SID
EARLY = 5
POLL = 0.5
REPORT_LINES = 20
REPLY_LINES = 80
PANE_LINES = 40
RUN = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
RUN_TAIL = 1 << 20
LIVE = "#{session_name}\t#{@sid}\t#{@state}\t#{@pane}\t#{@opener}"
NO_SERVER = ("no server running", "error connecting to")
LIVE_STATES = ("working", "done", "blocked", "dead")
ENDED = {"done": "finished", "failed": "finished", "needs_input": "waiting"}
CLIENTS = "#{client_activity} #{client_tty}"
HOSTS = "#{pane_tty}\t#{session_name}"
HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tpane"
ROTATE_MSG = "rotate: run crew.py attach, then arm with its N and GEN"


class WorkersError(Exception):
    pass


class NotAWorker(WorkersError):
    pass


class Refused(WorkersError):
    pass


def _run(proc, argv, **kw):
    try:
        return proc(argv, **{**RUN, **kw})
    except OSError as e:
        raise WorkersError(f"{argv[0]}: {e.strerror or e}") from e


def _check(name: str) -> None:
    if not NAME.fullmatch(name):
        raise WorkersError(f"bad name {name!r}: use [A-Za-z0-9_-]+")


def _status_line() -> bool:
    try:
        return repo.status_line(repo.read_config(os.path.join(CORE, "config.toml")))
    except (OSError, ValueError) as e:
        raise WorkersError(f"core config: {e}") from e


def _per_column() -> int | None:
    try:
        return repo.workers_per_column(repo.read_config(os.path.join(CORE, "config.toml")))
    except (OSError, ValueError) as e:
        raise WorkersError(f"core config: {e}") from e


def _retile() -> str | None:
    try:
        return repo.grid_retile(repo.read_config(os.path.join(CORE, "config.toml")))
    except (OSError, ValueError) as e:
        raise WorkersError(f"core config: {e}") from e


def _tmux(argv: list, proc) -> None:
    res = _run(proc, argv)
    if res.returncode != 0:
        raise WorkersError(f"tmux {argv[1]} {argv[-2]}: {(res.stderr or '').strip()}")


def _loaded() -> dict[str, type[clients.Client]]:
    try:
        return clients.loaded(CORE)
    except ConfigError as e:
        raise WorkersError(str(e)) from e


def _get(name: str) -> clients.Client:
    try:
        return clients.get(name, CORE)
    except ConfigError as e:
        raise WorkersError(str(e)) from e


def _client(name: str | None, env: dict, classes: dict) -> tuple[str, clients.Client]:
    if name is None:
        try:
            name = clients.caller(env, classes) or roster.LEGACY_CLIENT
        except ConfigError as e:
            raise WorkersError(str(e)) from e
    return name, _get(name)


def _command(client: clients.Client, spec: clients.WorkerSpec) -> clients.WorkerCommand:
    try:
        return client.worker(spec)
    except ConfigError as e:
        raise WorkersError(str(e)) from e


def _allowed(classes: dict) -> set[str]:
    return {k for cls in classes.values() for k, v in cls.command_env.items() if isinstance(v, str)}


def _override(item: str, allowed: set[str]) -> tuple[str, str]:
    key, eq, value = item.partition("=")
    if not eq or key not in allowed:
        raise WorkersError(
            f"--env {item}: want NAME=VALUE, NAME one of {', '.join(sorted(allowed))}"
        )
    if not roster.printable(value):
        raise WorkersError(
            f"--env {key}: {value!r}: want printable text (no control character, U+2028 or U+2029)"
        )
    return key, value


def _environment(env: dict, classes: dict, client_env: dict, overrides: dict) -> dict[str, str]:
    drop = set()
    for cls in classes.values():
        inside = bool(cls.session_env) and cls.session_env in env
        drop.update(
            k for k, v in cls.command_env.items() if v is None or (inside and env.get(k) == v)
        )
        if cls.session_env:
            drop.add(cls.session_env)
    return {**{k: v for k, v in env.items() if k not in drop}, **client_env, **overrides}


def _early(name: str, exe: str, proc) -> None:
    try:
        for _ in range(round(EARLY / POLL)):
            time.sleep(POLL)
            state = tmux_host.status(name, proc=proc)
            if state is None:
                raise WorkersError(f"{name}: session ended at once")
            if state != tmux_host.RUNNING:
                lines = [x for x in tmux_host.read(name, 2000, proc=proc).splitlines() if x.strip()]
                raise WorkersError(
                    f"{name}: {os.path.basename(exe)} exited {state} at once; its pane's last lines:\n"
                    + "\n".join(lines[-REPORT_LINES:])
                )
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e


def _dialog(name: str, client: clients.Client, events: str, proc) -> None:
    if not client.worker_dialogs:
        return
    try:
        screen = " ".join(tmux_host.read(name, proc=proc).split())
        text = next((t for t in client.worker_dialogs if " ".join(t.split()) in screen), None)
        if text is None:
            return
        pane = _query(
            ["tmux", "display-message", "-p", "-t", f"={name}:", "#{pane_id}"], proc
        ).strip()
        tmux_host.mark("blocked", events, pane=pane, proc=proc)
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e
    print(f"workers: {name}: waits on a dialog: {text}", file=sys.stderr)


def _note_text(text: str) -> str | None:
    if len(text) > roster.NOTE_MAX:
        raise WorkersError(f"note: {len(text)} characters: want at most {roster.NOTE_MAX}")
    if not roster.printable(text):
        raise WorkersError(
            f"note: {text!r}: want printable text (no control character, U+2028 or U+2029)"
        )
    return text or None


def _enroll(
    name: str,
    directory: str,
    own: str | None,
    *,
    sid: str,
    cwd: str,
    note: str | None,
    flags,
    client: str,
    overrides: dict,
    split: str | None,
    split_from: str | None,
    proc,
) -> None:
    resume = [
        sys.executable,
        os.path.abspath(__file__),
        "start",
        name,
        "--manager",
        os.path.basename(directory),
        "--client",
        client,
        "--resume",
        sid,
        "--cwd",
        cwd,
        *(w for k, v in overrides.items() for w in ("--env", f"{k}={v}")),
        *(["--split", split] if split is not None else []),
        *(["--split-from", split_from] if split_from is not None else []),
        *(["--", *flags] if flags else []),
    ]
    try:
        row = _live(proc).get(name)
    except WorkersError:
        row = None
    try:
        with _leased(directory, own, proc) as r:
            value = roster.entry(
                "worker",
                sid=sid,
                cwd=cwd,
                resume=resume,
                note=note,
                split=split,
                split_from=split_from,
            )
            roster.record(r, name, value if row is None else _placed(value, row))
    except WorkersError as e:
        roster.unwritten(name, e)


def start(
    name: str,
    directory: str,
    *,
    own: str | None,
    cwd: str,
    prompt: str | None = None,
    note: str | None = None,
    flags=(),
    env: dict,
    client: str | None = None,
    overrides=(),
    resume: str | None = None,
    split_from: str | None = None,
    split: str | None = None,
    proc=subprocess.run,
) -> str:
    _check(name)
    if split_from is not None:
        _check(split_from)
    if resume is not None and not SESSION_ID.fullmatch(resume):
        raise WorkersError(f"--resume {resume}: not a session id")
    if note is not None:
        note = _note_text(note)
    classes = _loaded()
    client, worker = _client(client, env, classes)
    overrides = dict(_override(item, _allowed(classes)) for item in overrides)
    with _leased(directory, own, proc, write=False):
        pass
    try:
        events = tmux_host.events_file(roster.events(directory, create=False))
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e
    status_line, per_column, retile = _status_line(), _per_column(), _retile()
    cwd = os.path.abspath(cwd)
    if not os.path.isdir(cwd):
        raise WorkersError(f"--cwd {cwd}: not a directory")
    sid = str(uuid.uuid4()) if resume is None else resume
    cmd = _command(
        worker,
        clients.WorkerSpec(
            name=name,
            sid=sid,
            cwd=cwd,
            flags=tuple(flags),
            prompt=prompt,
            events=events,
            resume=resume is not None,
            env=_environment(env, classes, {}, {}),
        ),
    )
    try:
        tmux_host.start(
            name,
            cmd.argv,
            cwd=cwd,
            env=_environment(env, classes, cmd.env, overrides),
            events=events,
            split=split,
            split_from=split_from,
            per_column=per_column,
            retile=retile,
            status_line=status_line,
            claude_hooks=False,
            proc=proc,
        )
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e
    options = {
        "@sid": sid,
        "@cwd": cwd,
        "@client": client,
        "@exe": cmd.argv[0],
        "@flags": json.dumps(list(flags)),
        "@env": json.dumps(overrides),
    }
    for argv in (["tmux", "set-option", "-t", f"={name}:", k, v] for k, v in options.items()):
        try:
            _tmux(argv, proc)
        except WorkersError as e:
            try:
                _run(proc, ["tmux", "kill-session", "-t", f"={name}"])
            except WorkersError:
                pass
            raise WorkersError(f"{e}; start undone") from e
    _enroll(
        name,
        directory,
        own,
        sid=sid,
        cwd=cwd,
        note=note,
        flags=flags,
        client=client,
        overrides=overrides,
        split=split,
        split_from=split_from,
        proc=proc,
    )
    _early(name, cmd.argv[0], proc)
    _dialog(name, worker, events, proc)
    return sid


def _option(name: str, key: str, proc, *, required: bool = True) -> str | None:
    res = _run(proc, ["tmux", "show-options", "-t", f"={name}:", "-v", key])
    if res.returncode != 0:
        if required:
            raise NotAWorker(f"{name}: not a worker")
        return None
    return res.stdout.rstrip("\n")


def _stored(name: str, allowed: set[str], proc) -> dict:
    opt = {k: _option(name, "@" + k, proc) for k in ("sid", "cwd", "events", "flags")}
    client = _option(name, "@client", proc, required=False)
    if client is None:
        opt.update(client=roster.LEGACY_CLIENT, exe=_option(name, "@claude", proc), env="{}")
    else:
        opt.update(client=client, exe=_option(name, "@exe", proc), env=_option(name, "@env", proc))
    try:
        flags, env = json.loads(opt["flags"]), json.loads(opt["env"])
        ok = (
            all(opt[k] for k in ("sid", "cwd", "events", "client", "exe"))
            and isinstance(flags, list)
            and all(isinstance(f, str) for f in flags)
            and isinstance(env, dict)
            and all(isinstance(v, str) for v in env.values())
        )
    except ValueError:
        ok = False
    if not ok:
        raise NotAWorker(f"{name}: not a worker")
    try:
        env = dict(_override(f"{k}={v}", allowed) for k, v in env.items())
    except WorkersError as e:
        raise WorkersError(f"{name}: @env: {e}") from e
    return {**opt, "flags": flags, "env": env}


def restart(name: str, *, env: dict, proc=subprocess.run) -> str:
    _check(name)
    classes = _loaded()
    opt = _stored(name, _allowed(classes), proc)
    worker = _get(opt["client"])
    status_line = _status_line()
    try:
        events = tmux_host.events_file(opt["events"])
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e
    command = _command(
        worker,
        clients.WorkerSpec(
            name=name,
            sid=opt["sid"],
            cwd=opt["cwd"],
            flags=tuple(opt["flags"]),
            prompt=None,
            events=events,
            resume=True,
            env=_environment(env, classes, {}, {}),
            exe=opt["exe"],
        ),
    )
    try:
        tmux_host.decorate(name, events, status_line=status_line, proc=proc)
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e
    cmd = shlex.join(command.argv)
    _tmux(["tmux", "set-option", "-t", f"={name}:", "@state", ""], proc)
    print(cmd, file=sys.stderr)
    try:
        tmux_host.respawn(
            name,
            command.argv,
            cwd=opt["cwd"],
            env=_environment(env, classes, command.env, opt["env"]),
            proc=proc,
        )
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e
    _early(name, command.argv[0], proc)
    _dialog(name, worker, events, proc)
    return cmd


def reply(name: str, *, proc=subprocess.run) -> str:
    _check(name)
    sid = _option(name, "@sid", proc)
    if not SESSION_ID.fullmatch(sid):
        raise WorkersError(f"{name}: bad session id {sid!r}")
    client = _option(name, "@client", proc, required=False)
    worker = _get(roster.LEGACY_CLIENT if client is None else client)
    try:
        return worker.worker_reply(sid)
    except ConfigError as e:
        raise WorkersError(str(e)) from e


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


def content(event: str, *, proc=subprocess.run) -> str:
    name, kind = event.split(" ")[1:3]
    if kind not in ("done", "blocked", "dead"):
        return ""
    if kind == "done":
        try:
            return "\n".join(reply(name, proc=proc).split("\n")[:REPLY_LINES])
        except NotAWorker:
            pass
    try:
        return tmux_host.read(name, PANE_LINES, proc=proc)
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e


def _query(argv: list, proc) -> str:
    res = _run(proc, argv)
    if res.returncode != 0:
        raise WorkersError(f"tmux {argv[1]}: {(res.stderr or '').strip()}")
    return res.stdout


def _live(proc) -> dict[str, dict]:
    res = _run(proc, ["tmux", "list-sessions", "-F", LIVE])
    if res.returncode != 0:
        err = (res.stderr or "").strip()
        if err.startswith(NO_SERVER):
            return {}
        raise WorkersError(f"tmux list-sessions: {err}")
    rows = (line.split("\t", 4) for line in res.stdout.split("\n"))
    return {
        f[0]: dict(zip(("sid", "state", "pane", "opener"), f[1:], strict=False))
        for f in rows
        if len(f) == 5 and NAME.fullmatch(f[0])
    }


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


def _placed(e: dict, options: dict) -> dict:
    for key in ("pane", "opener"):
        with contextlib.suppress(roster.RosterError):
            e = roster.entry(**{**e, key: options[key]})
    return e


def _place(r: dict, name: str, options: dict) -> None:
    r["entries"][name] = _placed(r["entries"][name], options)


def _sessions(name: str, e: dict, live: dict) -> tuple[str | None, str | None]:
    pane, driver = (name, None) if e["kind"] == "worker" else (e["tui"], name)
    return (pane if pane in live else None), (driver if driver in live else None)


def _sync(r: dict, live: dict) -> dict[str, str]:
    entries = r["entries"]
    for name in sorted(entries):
        sid = entries[name]["sid"]
        if entries[name]["kind"] == "worker" and sid is not None and name not in live:
            to = [n for n, o in live.items() if o["sid"] == sid]
            if len(to) == 1 and to[0] not in entries:
                entries[to[0]] = entries.pop(name)
    panes = {}
    for name in sorted(entries):
        e = entries[name]
        pane, driver = _sessions(name, e, live)
        if pane is None and driver is None:
            e["state"] = "gone" if e["kind"] == "worker" else ENDED.get(_status(e["cwd"]), "gone")
            continue
        state = live[pane]["state"] if pane else "working"
        e["state"] = state if state in LIVE_STATES else "working"
        if pane:
            _place(r, name, live[pane])
            panes[name] = pane
    return panes


def _reopen(
    panes: dict, entries: dict, per_column: int | None, retile: str | None, proc
) -> tuple[dict, list]:
    shown, opened, hosts = {}, [], None
    for name in sorted(panes):
        session, e = panes[name], entries[name]
        try:
            out = _query(["tmux", "list-clients", "-t", f"={session}", "-F", CLIENTS], proc)
        except WorkersError:
            continue
        clients = [line.partition(" ") for line in out.split("\n") if line]
        if not clients:
            try:
                tmux_host.show(
                    session,
                    split=e["split"],
                    split_from=e["split_from"],
                    per_column=per_column,
                    retile=retile,
                    proc=proc,
                )
            except tmux_host.TuiError as err:
                raise WorkersError(str(err)) from err
            opened.append((name, e["kind"], e["sid"], session))
            continue
        if hosts is None:
            try:
                rows = _query(["tmux", "list-panes", "-a", "-F", HOSTS], proc).split("\n")
            except WorkersError:
                rows = []
            hosts = dict(row.split("\t", 1) for row in rows if "\t" in row)
        tty = max(clients, key=lambda c: int(c[0]) if c[0].isascii() and c[0].isdigit() else -1)[2]
        host = hosts.get(tty, "") if tty else ""
        shown[name] = host if NAME.fullmatch(host) else "a terminal"
    return shown, opened


def _recovery(e: dict) -> str:
    argv = roster.recovery(e)
    if argv is None:
        return "none (no sid)"
    return (
        f"cd {shlex.quote(e['cwd'])} && {shlex.join(argv)}"
        if e["kind"] == "role"
        else shlex.join(argv)
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
    directory: str,
    own: str | None,
    *,
    after: int | None = None,
    gen: int | None = None,
    proc=subprocess.run,
) -> tuple[str, dict]:
    per_column, retile = _per_column(), _retile()
    try:
        events = roster.events(directory)
        with roster.locked(directory) as r:
            roster.take(r, directory, own, proc=proc)
            if own is None:
                print(
                    f"workers: not in tmux: no lease on {directory}; another manager may attach",
                    file=sys.stderr,
                )
            if after is not None:
                roster.advance(r, directory, after, gen)
            live = _live(
                proc
            )  # before the rotation: a raise after its rename would drop the new cursor and gen
            roster.rotate(r, directory)
            panes = _sync(r, live)
            backlog, cursor, gen = _backlog(events, r["cursor"]), r["cursor"], r["gen"]
        shown, opened = _reopen(panes, r["entries"], per_column, retile, proc)
        if opened:
            live = _live(proc)
            with roster.locked(directory) as r:
                for name, kind, sid, session in opened:
                    e = r["entries"].get(name)
                    if e is not None and (e["kind"], e["sid"]) == (kind, sid) and session in live:
                        _place(r, name, live[session])
    except roster.RosterError as e:
        raise WorkersError(str(e)) from e
    me, name = (
        shlex.quote(os.path.abspath(__file__)),
        shlex.quote(os.path.basename(os.path.dirname(events))),
    )
    arm = [
        f"N={cursor} GEN={gen}",
        f"monitor: tail -n +{cursor + 1} -F {shlex.quote(events)}",
        f"monitor expired: python3 {me} attach --manager {name} --after LINE --gen {gen} --resume",
        f"next-event: python3 {me} next-event --manager {name} --after {cursor} --gen {gen}",
    ]
    return _table(r["entries"], shown) + "".join(f"{line}\n" for line in backlog + arm), r[
        "entries"
    ]


def recover(entries: dict, *, proc=subprocess.run) -> Iterator[tuple[bool, str]]:
    for name in sorted(entries):
        e = entries[name]
        if e["state"] != "gone":
            continue
        argv = roster.recovery(e)
        if argv is None:
            yield False, f"resumed {name}: not run: no sid"
            continue
        try:
            res = proc(
                argv,
                cwd=e["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except OSError as err:
            yield False, f"resumed {name}: not run: {err.strerror or err}"
            continue
        except (
            Exception
        ) as err:  # e.g. Popen's ValueError for an argv item (a NUL, a lone surrogate)
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
def _leased(directory: str, own: str | None, proc, *, write: bool = True):
    try:
        with roster.locked(directory, write=write) as r:
            roster.check(r, directory, own, proc=proc)
            yield r
    except roster.RosterError as e:
        raise WorkersError(str(e)) from e


def release(directory: str, own: str | None, *, proc=subprocess.run) -> None:
    with _leased(directory, own, proc) as r:
        r["holder"] = None


def note(directory: str, own: str | None, name: str, text: str, *, proc=subprocess.run) -> None:
    _check(name)
    with _leased(directory, own, proc) as r:
        if name not in r["entries"]:
            raise WorkersError(f"{name} not in roster")
        r["entries"][name]["note"] = _note_text(text)


def stop(directory: str, own: str | None, name: str, *, proc=subprocess.run) -> None:
    _check(name)
    with _leased(directory, own, proc) as r:
        roster.stop(r, name, proc=proc)


def opened(own: str, live: dict[str, dict], entries=lambda session: {}) -> list[str]:
    depth, level, todo = {}, 0, {own}
    while todo:
        level += 1
        found = {n for n, o in live.items() if o["opener"] in todo}
        found.update(
            n
            for s in todo
            for k, e in entries(s).items()
            for n in (k, e["tui"])
            if n in live and live[n]["opener"] in ("", s)
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


def stop_all(
    own: str | None, *, dry_run: bool = False, proc=subprocess.run
) -> tuple[list[str], list[str]]:
    if own is None:
        raise WorkersError("stop-all: not in tmux: run it in the manager's tmux session")
    home = roster.directory(own, proc=proc)
    if os.path.isdir(home):
        with _leased(home, own, proc, write=False):
            pass
    read = {}

    def entries(session):
        if session not in read:
            read[session] = _entries(roster.directory(session, proc=proc))
        return read[session]

    chosen = opened(own, _live(proc), entries)
    pipelines = {k for es in read.values() for k, e in es.items() if e["kind"] == "pipeline"}
    verb, left = "would stop", []
    if not dry_run:
        verb = "stopped"
        for name in chosen:
            with contextlib.suppress(tmux_host.TuiError):
                tmux_host.kill(name, proc=proc)
        after = _live(proc)
        left = [n for n in chosen if n in after]
        gone = set(chosen) - set(left)
        for session in [s for s, es in read.items() if es]:
            d = roster.directory(session, proc=proc)
            try:
                with _leased(d, own, proc) if session == own else roster.locked(d) as r:
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


def forget(directory: str, own: str | None, name: str, *, proc=subprocess.run) -> None:
    _check(name)
    with _leased(directory, own, proc) as r:
        pane, driver = _sessions(name, roster.remove(r, name), _live(proc))
        if pane or driver:
            raise WorkersError(f"{name}: session {pane or driver} is live")


def _directory(name: str | None) -> str:
    try:
        directory = roster.directory(name, proc=subprocess.run)
    except roster.RosterError as e:
        raise WorkersError(str(e)) from e
    if directory is None:
        raise WorkersError("no manager directory: run inside tmux or give --manager <name>")
    return directory


def _events(name: str | None) -> str:
    directory = _directory(name)
    with _leased(directory, _own(), subprocess.run, write=False):
        pass
    return roster.events(directory, create=False)


def _record(
    directory: str, own: str | None, after: int | None, gen: int, proc
) -> tuple[str, int, bool]:
    with _leased(directory, own, proc) as r:
        try:
            after = roster.advance(r, directory, after, gen)
        except roster.Stale as e:
            raise Refused(str(e)) from e
        return roster.events(directory, create=False), after, roster.due(r, directory)


def _own() -> str | None:
    try:
        return tmux_host.own_session(proc=subprocess.run)
    except tmux_host.TuiError as e:
        raise WorkersError(str(e)) from e


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


def main(argv=None) -> int:
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
        help=f"the worker's client (default: the one whose session runs this, else {roster.LEGACY_CLIENT})",
    )
    p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="sets a client-declared default in the worker's env; repeatable",
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
            text, entries = attach(
                _directory(a.manager), _own(), after=a.after, gen=a.gen, proc=subprocess.run
            )
            print(text, end="", flush=True)
            if a.resume:
                failed = False
                for ok, line in recover(entries, proc=subprocess.run):
                    print(line, flush=True)
                    failed = failed or not ok
                if failed:
                    return 1
        elif a.cmd == "release":
            directory = _directory(a.manager)
            release(directory, _own(), proc=subprocess.run)
            print(f"workers: released {directory}")
        elif a.cmd == "note":
            note(_directory(a.manager), _own(), a.name, a.text, proc=subprocess.run)
        elif a.cmd == "stop":
            stop(_directory(a.manager), _own(), a.name, proc=subprocess.run)
        elif a.cmd == "stop-all":
            lines, left = stop_all(_own(), dry_run=a.dry_run, proc=subprocess.run)
            for line in lines or ([] if left else ["workers: stop-all: nothing to stop"]):
                print(line)
            if left:
                raise WorkersError(
                    f"stop-all: still live after tmux kill-session: {', '.join(left)}"
                )
        elif a.cmd == "forget":
            forget(_directory(a.manager), _own(), a.name, proc=subprocess.run)
        elif a.cmd == "start":
            sid = start(
                a.name,
                _directory(a.manager),
                own=_own(),
                cwd=a.cwd or os.getcwd(),
                prompt=a.prompt,
                note=a.note,
                flags=flags,
                env=dict(os.environ),
                client=a.client,
                overrides=a.env,
                resume=a.resume,
                split_from=a.split_from,
                split=a.split,
                proc=subprocess.run,
            )
            print(f"{a.name} {sid}")
        elif a.cmd == "restart":
            restart(a.name, env=dict(os.environ), proc=subprocess.run)
        elif a.cmd == "reply":
            text = reply(a.name, proc=subprocess.run)
            if text:
                print(text)
        else:
            events, after, due = _record(
                _directory(a.manager), _own(), a.after, a.gen, subprocess.run
            )
            if due:
                raise Refused(ROTATE_MSG)
            line, event = next_event(events, after)
            print(f"{line} {event}")
            try:
                text = content(event, proc=subprocess.run)
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
