"""Starts and directs Claude Code workers in tmux panes through the plugin's src/tui_claude.py. Stdlib only."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(CORE, "src"))
import repo  # noqa: E402
import tui_claude  # noqa: E402

NAME = re.compile(r"[A-Za-z0-9_-]+")
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")
STRIP = (*tui_claude.PARENT_KEYS, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
         "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_EFFORT")
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REFUSED = ("--resume", "-r", "--session-id", "--continue", "-c", "--fork-session", "--from-pr", "--teleport")
EARLY = 5
POLL = 0.5
REPORT_LINES = 20
REPLY_LINES = 80
PANE_LINES = 40
BLOCK = 1 << 16
RUN = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}


class WorkersError(Exception):
    pass


class NotAWorker(WorkersError):
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
    """Core config's status_line (repo.read_config)."""
    try:
        return repo.status_line(repo.read_config(os.path.join(CORE, "config.toml")))
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


def start(name: str, events: str, *, cwd: str, prompt: str | None = None, flags=(), env: dict,
          resume: str | None = None, split_from: str | None = None, split: str | None = None,
          proc=subprocess.run) -> str:
    """Start worker `name`, record its options on the tmux session; returns the session id.
    `resume` (a session id) resumes that session instead of starting a new one. `split_from` or `split` replaces
    tui_claude's automatic placement; it defaults the other. Its status line: core config's status_line. A claude that
    exits within EARLY seconds raises, the session kept (see _early)."""
    _check(name)
    if split_from is not None:
        _check(split_from)
    if resume is not None and not SESSION_ID.fullmatch(resume):
        raise WorkersError(f"--resume {resume}: not a session id")
    _refuse(flags)
    status_line = _status_line()
    events, cwd = os.path.abspath(events), os.path.abspath(cwd)
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
                         cwd=cwd, env=child_env, events=events, split=split, split_from=split_from, status_line=status_line,
                         proc=proc)
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


def _after(value: str) -> int | None:
    if value == "end":
        return None
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise argparse.ArgumentTypeError(f"{value!r}: use a line number or end")


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
    p.add_argument("--events", required=True)
    p.add_argument("--cwd")
    p.add_argument("--prompt")
    p.add_argument("--resume", metavar="SID", help="a session id")
    p.add_argument("--split-from")
    p.add_argument("--split", choices=("right", "below"))
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
    p = sub.add_parser("next-event")
    p.add_argument("--events", required=True)
    p.add_argument("--after", type=_after, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            sid = start(a.name, a.events, cwd=a.cwd or os.getcwd(), prompt=a.prompt, flags=flags,
                        env=dict(os.environ), resume=a.resume, split_from=a.split_from, split=a.split, proc=subprocess.run)
            print(f"{a.name} {sid}")
        elif a.cmd == "restart":
            restart(a.name, env=dict(os.environ), proc=subprocess.run)
        elif a.cmd == "reply":
            text = reply(a.name, proc=subprocess.run)
            if text:
                print(text)
        else:
            line, event = next_event(a.events, a.after)
            print(f"{line} {event}")
            try:
                text = content(event, proc=subprocess.run)
            except WorkersError as e:
                text = f"workers: {e}"   # stdout: background readers watch only it
            if text:
                print(text)
    except WorkersError as e:
        print(f"workers: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
