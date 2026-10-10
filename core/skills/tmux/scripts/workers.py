"""Starts and directs Claude Code workers in tmux panes through the plugin's src/tui_claude.py. Stdlib only."""
from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator

CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(CORE, "src"))
import manager  # noqa: E402
import repo  # noqa: E402
import tui_claude  # noqa: E402

NAME = re.compile(r"[A-Za-z0-9_-]+")
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")
STRIP = (*tui_claude.PARENT_KEYS, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
         "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_EFFORT")
SESSION_ID = manager.SID
REFUSED = ("--resume", "-r", "--session-id", "--continue", "-c", "--fork-session", "--from-pr", "--teleport")
EARLY = 5
POLL = 0.5
REPORT_LINES = 20
REPLY_LINES = 80
PANE_LINES = 40
BLOCK = 1 << 16
RUN = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
RUN_TAIL = 1 << 20
LIVE = "#{session_name}\t#{@sid}\t#{@state}\t#{@pane}\t#{@opener}"
NO_SERVER = ("no server running", "error connecting to")
LIVE_STATES = ("working", "done", "blocked", "dead")
ENDED = {"done": "finished", "failed": "finished", "needs_input": "waiting"}
CLIENTS = "#{client_activity} #{client_tty}"
HOSTS = "#{pane_tty}\t#{session_name}"
HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tpane"
ROTATE_MSG = "rotate: run workers.py attach, then arm with its N and GEN"


class WorkersError(Exception):
    pass


class NotAWorker(WorkersError):
    pass


class Refused(WorkersError):
    """A refusal next-event prints on stdout: background readers watch only it."""


def _run(proc, argv, **kw):
    try:
        return proc(argv, **{**RUN, **kw})
    except OSError as e:
        raise WorkersError(f"{argv[0]}: {e.strerror or e}") from e


def _check(name: str) -> None:
    if not NAME.fullmatch(name):
        raise WorkersError(f"bad name {name!r}: use [A-Za-z0-9_-]+")


def _status_line() -> bool:
    """Core config's status_line (repo.read_config)."""
    try:
        return repo.status_line(repo.read_config(os.path.join(CORE, "config.toml")))
    except (OSError, ValueError) as e:
        raise WorkersError(f"core config: {e}") from e


def _per_column() -> int | None:
    """Core config's workers_per_column (repo.read_config)."""
    try:
        return repo.workers_per_column(repo.read_config(os.path.join(CORE, "config.toml")))
    except (OSError, ValueError) as e:
        raise WorkersError(f"core config: {e}") from e


def _retile() -> str | None:
    """Core config's grid_retile (repo.read_config)."""
    try:
        return repo.grid_retile(repo.read_config(os.path.join(CORE, "config.toml")))
    except (OSError, ValueError) as e:
        raise WorkersError(f"core config: {e}") from e


def _tmux(argv: list, proc) -> None:
    res = _run(proc, argv)
    if res.returncode != 0:
        raise WorkersError(f"tmux {argv[1]} {argv[-2]}: {(res.stderr or '').strip()}")


def _refuse(flags) -> None:
    """Raises on a flag, before the first bare `--`, that makes claude pick the session."""
    for flag in flags:
        if flag == "--":
            return
        word = flag.split("=", 1)[0] if flag.startswith("--") else flag
        if word in REFUSED:
            raise WorkersError(f"{word}: workers.py picks the session; use start --resume <sid>")


def _early(name: str, proc) -> None:
    """Watches worker `name`'s pane for EARLY seconds; raises if claude exits or the session ends, the exit with the
    pane's last REPORT_LINES non-blank lines."""
    try:
        for _ in range(round(EARLY / POLL)):
            time.sleep(POLL)
            state = tui_claude.status(name, proc=proc)
            if state is None:
                raise WorkersError(f"{name}: session ended at once")
            if state != tui_claude.RUNNING:
                lines = [x for x in tui_claude.read(name, 2000, proc=proc).splitlines() if x.strip()]
                raise WorkersError(f"{name}: claude exited {state} at once; its pane's last lines:\n"
                                   + "\n".join(lines[-REPORT_LINES:]))
    except tui_claude.TuiError as e:
        raise WorkersError(str(e)) from e


def _note_text(text: str) -> str | None:
    """A note's text, checked (manager.NOTE_MAX, manager.printable); "" is None."""
    if len(text) > manager.NOTE_MAX:
        raise WorkersError(f"note: {len(text)} characters: want at most {manager.NOTE_MAX}")
    if not manager.printable(text):
        raise WorkersError(f"note: {text!r}: want printable text (no control character, U+2028 or U+2029)")
    return text or None


def _enroll(name: str, directory: str, own: str | None, *, sid: str, cwd: str, note: str | None, flags,
            split: str | None, split_from: str | None, proc) -> None:
    """Writes worker `name`'s roster entry (manager.record) in one lease-checked block: its recovery argv, start
    --resume <sid>, and its session's placement (_placed). A failure only prints its line (manager.unwritten)."""
    resume = [sys.executable, os.path.abspath(__file__), "start", name, "--manager", os.path.basename(directory),
              "--resume", sid, "--cwd", cwd, *(["--split", split] if split is not None else []),
              *(["--split-from", split_from] if split_from is not None else []), *(["--", *flags] if flags else [])]
    try:
        row = _live(proc).get(name)
    except WorkersError:
        row = None
    try:
        with _leased(directory, own, proc) as r:
            value = manager.entry("worker", sid=sid, cwd=cwd, resume=resume, note=note, split=split,
                                  split_from=split_from)
            manager.record(r, name, value if row is None else _placed(value, row))
    except WorkersError as e:
        manager.unwritten(name, e)


def start(name: str, directory: str, *, own: str | None, cwd: str, prompt: str | None = None,
          note: str | None = None, flags=(), env: dict, resume: str | None = None, split_from: str | None = None,
          split: str | None = None, proc=subprocess.run) -> str:
    """Start worker `name` for manager directory `directory`, its lease checked for `own` (_leased) and its events
    file used; record its options on the tmux session, then its roster entry (_enroll); returns the session id.
    `resume` (a session id) resumes that session instead of starting a new one. `note` ("": none) is the entry's.
    `split_from` or `split` replaces tui_claude's automatic placement outside a tmux grid, which ignores them; it
    defaults the other. Its status line, grid column size and grid re-tiling: core config's status_line,
    workers_per_column and grid_retile. A claude that exits within EARLY seconds raises, the session and the entry
    kept (see _early)."""
    _check(name)
    if split_from is not None:
        _check(split_from)
    if resume is not None and not SESSION_ID.fullmatch(resume):
        raise WorkersError(f"--resume {resume}: not a session id")
    _refuse(flags)
    if note is not None:
        note = _note_text(note)
    with _leased(directory, own, proc, write=False):
        pass
    events = manager.events(directory, create=False)
    status_line, per_column, retile = _status_line(), _per_column(), _retile()
    cwd = os.path.abspath(cwd)
    if not os.path.isdir(cwd):
        raise WorkersError(f"--cwd {cwd}: not a directory")
    claude = shutil.which("claude", path=env.get("PATH"))
    if claude is None:
        raise WorkersError("claude not found on PATH")
    claude = os.path.abspath(claude)
    pick, sid = ("--session-id", str(uuid.uuid4())) if resume is None else ("--resume", resume)
    child_env = {k: v for k, v in env.items() if k not in STRIP}
    try:
        tui_claude.start(name, [claude, pick, sid, "--name", name, *flags,
                                *(["--", prompt] if prompt else [])],
                         cwd=cwd, env=child_env, events=events, split=split, split_from=split_from, per_column=per_column,
                         retile=retile, status_line=status_line, proc=proc)
    except tui_claude.TuiError as e:
        raise WorkersError(str(e)) from e
    options = {"@sid": sid, "@cwd": cwd, "@claude": claude, "@flags": json.dumps(list(flags))}
    for argv in (["tmux", "set-option", "-t", f"={name}:", k, v] for k, v in options.items()):
        try:
            _tmux(argv, proc)
        except WorkersError as e:
            try:
                _run(proc, ["tmux", "kill-session", "-t", f"={name}"])
            except WorkersError:
                pass
            raise WorkersError(f"{e}; start undone") from e
    _enroll(name, directory, own, sid=sid, cwd=cwd, note=note, flags=flags, split=split, split_from=split_from,
            proc=proc)
    _early(name, proc)
    return sid


def _option(name: str, key: str, proc) -> str:
    res = _run(proc, ["tmux", "show-options", "-t", f"={name}:", "-v", key])
    if res.returncode != 0:
        raise NotAWorker(f"{name}: not a worker")
    return res.stdout.rstrip("\n")


def _stored(name: str, proc) -> dict:
    opt = {k: _option(name, "@" + k, proc) for k in ("sid", "cwd", "events", "claude", "flags")}
    try:
        flags = json.loads(opt["flags"])
        ok = (all(opt[k] for k in ("sid", "cwd", "events", "claude"))
              and isinstance(flags, list) and all(isinstance(f, str) for f in flags))
    except ValueError:
        ok = False
    if not ok:
        raise NotAWorker(f"{name}: not a worker")
    return {**opt, "flags": flags}


def restart(name: str, *, env: dict, proc=subprocess.run) -> str:
    """Respawn worker `name`'s pane, its history cleared, resuming its session (tui_claude.respawn) with env minus
    STRIP, its status line as core config's status_line says now; returns the command, shell-quoted. A claude that
    exits within EARLY seconds raises (see _early)."""
    _check(name)
    opt = _stored(name, proc)
    status_line = _status_line()
    try:
        resume = tui_claude.with_hooks([opt["claude"], "--resume", opt["sid"], "--name", name, *opt["flags"]],
                                       opt["events"])
        tui_claude.decorate(name, opt["events"], status_line=status_line, proc=proc)
    except tui_claude.TuiError as e:
        raise WorkersError(str(e)) from e
    cmd = " ".join(shlex.quote(w) for w in resume)
    _tmux(["tmux", "set-option", "-t", f"={name}:", "@state", ""], proc)
    print(cmd, file=sys.stderr)
    try:
        tui_claude.respawn(name, resume, cwd=opt["cwd"], env={k: v for k, v in env.items() if k not in STRIP},
                           proc=proc)
    except tui_claude.TuiError as e:
        raise WorkersError(str(e)) from e
    _early(name, proc)
    return cmd


def reply(name: str, *, proc=subprocess.run, config: str | None = None) -> str:
    """The text blocks of worker `name`'s last text-bearing assistant message, joined by newlines; the transcript
    read from its end back to that message's first record."""
    _check(name)
    sid = _option(name, "@sid", proc)
    if not SESSION_ID.fullmatch(sid):
        raise WorkersError(f"{name}: bad session id {sid!r}")
    config = config or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    found = glob.glob(os.path.join(glob.escape(config), "projects", "*", sid + ".jsonl"))
    if not found:
        raise WorkersError(f"no transcript for {sid}: started with a leaked Claude Code variable?")
    last, texts = None, []
    try:
        with open(found[0], "rb") as f:
            for raw in _lines_back(f):
                try:
                    entry = json.loads(raw.decode("utf-8", errors="replace"))
                    message = entry["message"]
                    content = message["content"]
                    if entry["type"] != "assistant" or not isinstance(content, list):
                        continue
                    blocks = [b["text"] for b in content if b["type"] == "text"]
                    mid = message.get("id") or object()   # no id: a message of its own
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if last is None:
                    if blocks:
                        last, texts = mid, blocks
                elif mid != last:
                    break
                else:
                    texts = blocks + texts
    except OSError as e:
        raise WorkersError(f"transcript {found[0]}: {e.strerror}") from e
    return "\n".join(texts)


def _lines_back(f):
    """The binary file f's lines, last first, each without its newline; read from the end in BLOCK-byte reads."""
    pos = f.seek(0, os.SEEK_END)
    tail = []   # the pieces, last first, of the line whose start isn't read yet
    while pos > 0:
        size = min(BLOCK, pos)
        pos -= size
        f.seek(pos)
        parts = f.read(size).split(b"\n")
        tail.append(parts[-1])
        if len(parts) > 1:
            yield b"".join(reversed(tail))
            yield from reversed(parts[1:-1])
            tail = [parts[0]]
    yield b"".join(reversed(tail))


def next_event(events: str, after: int | None) -> tuple[int, str]:
    """Wait for the first event after line `after` of the events file (None: after the complete lines it holds now);
    returns (its line number, its text). Stops with WorkersError if the file can't be read, shrinks or is replaced."""
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
        complete = data[:data.rfind(b"\n") + 1]
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


def content(event: str, *, proc=subprocess.run, config: str | None = None) -> str:
    """What next-event prints after the event: a worker's done → the first REPLY_LINES lines of its reply; blocked,
    dead or a role run's done (not a worker) → the pane's last PANE_LINES lines, history included; else ""."""
    name, kind = event.split(" ")[1:3]
    if kind not in ("done", "blocked", "dead"):
        return ""
    if kind == "done":
        try:
            return "\n".join(reply(name, proc=proc, config=config).split("\n")[:REPLY_LINES])
        except NotAWorker:
            pass
    try:
        return tui_claude.read(name, PANE_LINES, proc=proc)
    except tui_claude.TuiError as e:
        raise WorkersError(str(e)) from e


def _query(argv: list, proc) -> str:
    """A tmux command's stdout; WorkersError when it fails."""
    res = _run(proc, argv)
    if res.returncode != 0:
        raise WorkersError(f"tmux {argv[1]}: {(res.stderr or '').strip()}")
    return res.stdout


def _live(proc) -> dict[str, dict]:
    """The live tmux sessions named by NAME, each to its LIVE options (sid, state, pane, opener); none when no tmux
    server runs."""
    res = _run(proc, ["tmux", "list-sessions", "-F", LIVE])
    if res.returncode != 0:
        err = (res.stderr or "").strip()
        if err.startswith(NO_SERVER):
            return {}
        raise WorkersError(f"tmux list-sessions: {err}")
    rows = (line.split("\t", 4) for line in res.stdout.split("\n"))
    return {f[0]: dict(zip(("sid", "state", "pane", "opener"), f[1:]))
            for f in rows if len(f) == 5 and NAME.fullmatch(f[0])}


def _status(cwd: str) -> str | None:
    """The outcome status of the last `result` line in the last RUN_TAIL bytes of <cwd>/run.jsonl (a regular file,
    never through a symlink); None when that line has no outcome with a string status, or there is none (an unreadable
    file included)."""
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
            lines = f.read(RUN_TAIL + 1).split(b"\n")[1 if at else 0:]
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
    """Entry e with a live session's pane and opener, each only when valid (manager.entry)."""
    for key in ("pane", "opener"):
        with contextlib.suppress(manager.ManagerError):
            e = manager.entry(**{**e, key: options[key]})
    return e


def _place(r: dict, name: str, options: dict) -> None:
    """Entry `name`'s pane and opener from a live session's (_placed)."""
    r["entries"][name] = _placed(r["entries"][name], options)


def _sessions(name: str, e: dict, live: dict) -> tuple[str | None, str | None]:
    """Entry `name`'s live sessions, None where not live: its pane session (a worker's own, a role's or pipeline's tui)
    and a role's or pipeline's driver (the session of its name). Either live: the entry is."""
    pane, driver = (name, None) if e["kind"] == "worker" else (e["tui"], name)
    return (pane if pane in live else None), (driver if driver in live else None)


def _sync(r: dict, live: dict) -> dict[str, str]:
    """Syncs roster r's entries with the live sessions; returns each live entry's pane session (_sessions) by entry
    name. A worker whose session is gone takes the name of the one live session with its sid, when no entry has that
    name. With no live session a worker is gone, a role or pipeline ENDED's state for its run's last outcome status
    (_status), else gone."""
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


def _reopen(panes: dict, entries: dict, per_column: int | None, retile: str | None, proc) -> tuple[dict, list]:
    """Reopens each pane session no client shows (tui_claude.show, beside the caller). Returns, by entry name, where
    each other one is shown: the session of the tmux pane its most recently active client runs in, when a NAME, else
    (list-panes failing too) `a terminal`; and the reopened entries' (name, kind, sid, session). A session whose
    list-clients fails is skipped."""
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
                tui_claude.show(session, split=e["split"], split_from=e["split_from"], per_column=per_column,
                                retile=retile, proc=proc)
            except tui_claude.TuiError as err:
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
    """The printed command that resumes gone entry e (manager.recovery)."""
    argv = manager.recovery(e)
    if argv is None:
        return "none (no sid)"
    return f"cd {shlex.quote(e['cwd'])} && {shlex.join(argv)}" if e["kind"] == "role" else shlex.join(argv)


def _table(entries: dict, shown: dict) -> str:
    """HEADER, a row per entry by name (null: -), `shown in <where>` last where shown says, then a `resume` line per
    gone entry."""
    lines = [HEADER]
    for name in sorted(entries):
        e = entries[name]
        cells = [name, *("-" if e[k] is None else e[k] for k in HEADER.split("\t")[1:])]
        lines.append("\t".join(cells + ([f"shown in {shown[name]}"] if name in shown else [])))
    lines += [f"resume {name}: {_recovery(entries[name])}" for name in sorted(entries)
              if entries[name]["state"] == "gone"]
    return "".join(f"{line}\n" for line in lines)


def _backlog(events: str, cursor: int) -> list[str]:
    """The event lines (EVENT) of `events` after line `cursor` (manager.following), each `<line> <text>` as next-event
    prints it; a trailing fragment is not a line yet."""
    texts = (raw.decode("utf-8", errors="replace") for raw in manager.following(events, cursor).split(b"\n")[:-1])
    return [f"{n} {text}" for n, text in enumerate(texts, cursor + 1) if EVENT.fullmatch(text)]


def attach(directory: str, own: str | None, *, after: int | None = None, gen: int | None = None,
           proc=subprocess.run) -> tuple[str, dict]:
    """Attaches the manager in tmux session `own` (None: outside tmux, no lease) to its directory, made with its events
    file. In one roster() block: takes the lease (manager.take); with `after`, line `after` of generation `gen` (the
    Monitor re-arm), records it (manager.advance; stale: refused, nothing written); rotates the events file when due
    (manager.rotate); syncs the entries (_sync); reads the backlog (_backlog), cursor and gen. Then, the lock released,
    reopens their unshown panes (_reopen) and records the new placement in a second block for each entry still of the
    same kind and sid. Returns the table (_table), the backlog, `N=<cursor> GEN=<gen>` and the arm commands; and the
    entries as the table shows them."""
    per_column, retile = _per_column(), _retile()
    try:
        events = manager.events(directory)
        with manager.roster(directory) as r:
            manager.take(r, directory, own, proc=proc)
            if own is None:
                print(f"workers: not in tmux: no lease on {directory}; another manager may attach", file=sys.stderr)
            if after is not None:
                manager.advance(r, directory, after, gen)
            live = _live(proc)   # before the rotation: a raise after its rename would drop the new cursor and gen
            manager.rotate(r, directory)
            panes = _sync(r, live)
            backlog, cursor, gen = _backlog(events, r["cursor"]), r["cursor"], r["gen"]
        shown, opened = _reopen(panes, r["entries"], per_column, retile, proc)
        if opened:
            live = _live(proc)
            with manager.roster(directory) as r:
                for name, kind, sid, session in opened:
                    e = r["entries"].get(name)
                    if e is not None and (e["kind"], e["sid"]) == (kind, sid) and session in live:
                        _place(r, name, live[session])
    except manager.ManagerError as e:
        raise WorkersError(str(e)) from e
    me, name = shlex.quote(os.path.abspath(__file__)), shlex.quote(os.path.basename(os.path.dirname(events)))
    arm = [f"N={cursor} GEN={gen}", f"monitor: tail -n +{cursor + 1} -F {shlex.quote(events)}",
           f"monitor expired: python3 {me} attach --manager {name} --after LINE --gen {gen} --resume",
           f"next-event: python3 {me} next-event --manager {name} --after {cursor} --gen {gen}"]
    return _table(r["entries"], shown) + "".join(f"{line}\n" for line in backlog + arm), r["entries"]


def recover(entries: dict, *, proc=subprocess.run) -> Iterator[tuple[bool, str]]:
    """Runs each gone entry's recovery argv (manager.recovery), by name, in the entry's cwd, never through a shell;
    yields (whether it ran and exited 0, its line) per entry as it ends: `resumed <name>: exit <code>[: <its last
    output line>]` (stdout and stderr together), or `resumed <name>: not run: <why>`. One failing, by any exception,
    stops none."""
    for name in sorted(entries):
        e = entries[name]
        if e["state"] != "gone":
            continue
        argv = manager.recovery(e)
        if argv is None:
            yield False, f"resumed {name}: not run: no sid"
            continue
        try:
            res = proc(argv, cwd=e["cwd"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace")
        except OSError as err:
            yield False, f"resumed {name}: not run: {err.strerror or err}"
            continue
        except Exception as err:   # e.g. Popen's ValueError for an argv item (a NUL, a lone surrogate)
            yield False, f"resumed {name}: not run: {type(err).__name__}: {err}"
            continue
        last = next((line.strip() for line in reversed(res.stdout.splitlines()) if line.strip()), "")
        yield res.returncode == 0, f"resumed {name}: exit {res.returncode}" + (f": {last}" if last else "")


@contextlib.contextmanager
def _leased(directory: str, own: str | None, proc, *, write: bool = True):
    """directory's roster() block, its lease checked first (manager.check); a ManagerError as a WorkersError."""
    try:
        with manager.roster(directory, write=write) as r:
            manager.check(r, directory, own, proc=proc)
            yield r
    except manager.ManagerError as e:
        raise WorkersError(str(e)) from e


def release(directory: str, own: str | None, *, proc=subprocess.run) -> None:
    """Gives up the lease on directory: no holder, the cursor kept."""
    with _leased(directory, own, proc) as r:
        r["holder"] = None


def note(directory: str, own: str | None, name: str, text: str, *, proc=subprocess.run) -> None:
    """Sets entry `name`'s note to text; "" clears it."""
    _check(name)
    with _leased(directory, own, proc) as r:
        if name not in r["entries"]:
            raise WorkersError(f"{name} not in roster")
        r["entries"][name]["note"] = _note_text(text)


def stop(directory: str, own: str | None, name: str, *, proc=subprocess.run) -> None:
    """Kills entry `name`'s sessions and removes it (manager.stop)."""
    _check(name)
    with _leased(directory, own, proc) as r:
        manager.stop(r, name, proc=proc)


def forget(directory: str, own: str | None, name: str, *, proc=subprocess.run) -> None:
    """Removes entry `name`; refused, nothing written, while a session keeps it live (_sessions)."""
    _check(name)
    with _leased(directory, own, proc) as r:
        pane, driver = _sessions(name, manager.remove(r, name), _live(proc))
        if pane or driver:
            raise WorkersError(f"{name}: session {pane or driver} is live")


def _directory(name: str | None) -> str:
    """Manager `name`'s directory, the filesystem untouched; `name` None: the caller's tmux session's
    (manager.directory). Raises outside tmux without a name."""
    try:
        directory = manager.directory(name, proc=subprocess.run)
    except manager.ManagerError as e:
        raise WorkersError(str(e)) from e
    if directory is None:
        raise WorkersError("no manager directory: run inside tmux or give --manager <name>")
    return directory


def _events(name: str | None) -> str:
    """The events file of _directory(name), once its lease check passes (_leased); makes nothing but roster.lock."""
    directory = _directory(name)
    with _leased(directory, _own(), subprocess.run, write=False):
        pass
    return manager.events(directory, create=False)


def _record(directory: str, own: str | None, after: int | None, gen: int, proc) -> tuple[str, int, bool]:
    """next-event's step, in one roster() block, its lease checked first (_leased): records line `after` of generation
    `gen` (None: the last complete line) as handled (manager.advance; stale: Refused, nothing written). Returns
    (the events file, `after` resolved, whether a rotation is due)."""
    with _leased(directory, own, proc) as r:
        try:
            after = manager.advance(r, directory, after, gen)
        except manager.Stale as e:
            raise Refused(str(e)) from e
        return manager.events(directory, create=False), after, manager.due(r, directory)


def _own() -> str | None:
    """The caller's tmux session (tui_claude.own_session); None outside tmux."""
    try:
        return tui_claude.own_session(proc=subprocess.run)
    except tui_claude.TuiError as e:
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
        argv, flags = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="workers.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("name")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p.add_argument("--cwd")
    p.add_argument("--prompt")
    p.add_argument("--resume", metavar="SID", help="a session id")
    p.add_argument("--note", help="its roster note, as workers.py note sets it")
    p.add_argument("--split-from")
    p.add_argument("--split", choices=("right", "below"))
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
    p = sub.add_parser("next-event")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p.add_argument("--after", type=_after, required=True)
    p.add_argument("--gen", type=_number, required=True)
    p = sub.add_parser("attach")
    p.add_argument("--manager", help="manager directory to attach to (default: your tmux session's)")
    p.add_argument("--after", type=_number, metavar="LINE", help="the line of the last event handled; with --gen")
    p.add_argument("--gen", type=_number, metavar="G", help="the generation of that line; with --after")
    p.add_argument("--resume", action="store_true", help="then run each gone entry's recovery command")
    p = sub.add_parser("release")
    p.add_argument("--manager", help="manager directory to release (default: your tmux session's)")
    p = sub.add_parser("note")
    p.add_argument("name")
    p.add_argument("text", help="'' clears it")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p = sub.add_parser("stop")
    p.add_argument("name", help="a roster entry, or its tui session")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p = sub.add_parser("forget")
    p.add_argument("name")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    a = ap.parse_args(argv)
    if a.cmd == "attach" and (a.after is None) != (a.gen is None):
        ap.error("--after and --gen go together")
    try:
        if a.cmd == "attach":
            text, entries = attach(_directory(a.manager), _own(), after=a.after, gen=a.gen, proc=subprocess.run)
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
        elif a.cmd == "forget":
            forget(_directory(a.manager), _own(), a.name, proc=subprocess.run)
        elif a.cmd == "start":
            sid = start(a.name, _directory(a.manager), own=_own(), cwd=a.cwd or os.getcwd(), prompt=a.prompt,
                        note=a.note, flags=flags, env=dict(os.environ), resume=a.resume, split_from=a.split_from,
                        split=a.split, proc=subprocess.run)
            print(f"{a.name} {sid}")
        elif a.cmd == "restart":
            restart(a.name, env=dict(os.environ), proc=subprocess.run)
        elif a.cmd == "reply":
            text = reply(a.name, proc=subprocess.run)
            if text:
                print(text)
        else:
            events, after, due = _record(_directory(a.manager), _own(), a.after, a.gen, subprocess.run)
            if due:
                raise Refused(ROTATE_MSG)
            line, event = next_event(events, after)
            print(f"{line} {event}")
            try:
                text = content(event, proc=subprocess.run)
            except WorkersError as e:
                text = f"workers: {e}"   # stdout: background readers watch only it
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
