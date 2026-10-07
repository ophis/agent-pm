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
import uuid

CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(CORE, "src"))
import repo  # noqa: E402
import tui_claude  # noqa: E402

NAME = re.compile(r"[A-Za-z0-9_-]+")
STRIP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
         "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_EFFORT")
ENV_KEYS = ("PATH", "CLAUDE_CONFIG_DIR")
RUN = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}


class WorkersError(Exception):
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


def start(name: str, events: str, *, cwd: str, prompt: str | None = None, flags=(), env: dict,
          anchor: str | None = None, split: str | None = None, proc=subprocess.run) -> str:
    """Start worker `name`, record its options on the tmux session; returns the session id.
    `anchor` or `split` replaces tui_claude's automatic placement; it defaults the other. Its status line: core config's
    status_line."""
    _check(name)
    if anchor is not None:
        _check(anchor)
    status_line = _status_line()
    events, cwd = os.path.abspath(events), os.path.abspath(cwd)
    if not os.path.isdir(cwd):
        raise WorkersError(f"--cwd {cwd}: not a directory")
    claude = shutil.which("claude", path=env.get("PATH"))
    if claude is None:
        raise WorkersError("claude not found on PATH")
    claude = os.path.abspath(claude)
    sid = str(uuid.uuid4())
    child_env = {k: v for k, v in env.items() if k not in STRIP}
    try:
        tui_claude.start(name, [claude, "--session-id", sid, "--name", name, *flags,
                                *(["--", prompt] if prompt else [])],
                         cwd=cwd, env=child_env, events=events, split=split, anchor=anchor, status_line=status_line,
                         proc=proc)
    except tui_claude.TuiError as e:
        raise WorkersError(str(e)) from e
    options = {"@sid": sid, "@cwd": cwd, "@claude": claude,
               "@env": json.dumps({k: child_env[k] for k in ENV_KEYS if k in child_env}),
               "@flags": json.dumps(list(flags))}
    for argv in (["tmux", "set-option", "-t", f"={name}:", k, v] for k, v in options.items()):
        try:
            _tmux(argv, proc)
        except WorkersError as e:
            try:
                _run(proc, ["tmux", "kill-session", "-t", f"={name}"])
            except WorkersError:
                pass
            raise WorkersError(f"{e}; start undone") from e
    return sid


def _option(name: str, key: str, proc) -> str:
    res = _run(proc, ["tmux", "show-options", "-t", f"={name}:", "-v", key])
    if res.returncode != 0:
        raise WorkersError(f"{name}: not a worker")
    return res.stdout.rstrip("\n")


def _stored(name: str, proc) -> dict:
    opt = {k: _option(name, "@" + k, proc) for k in ("sid", "cwd", "events", "claude", "env", "flags")}
    try:
        env, flags = json.loads(opt["env"]), json.loads(opt["flags"])
        ok = (all(opt[k] for k in ("sid", "cwd", "events", "claude")) and isinstance(env, dict)
              and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
              and isinstance(flags, list) and all(isinstance(f, str) for f in flags))
    except ValueError:
        ok = False
    if not ok:
        raise WorkersError(f"{name}: not a worker")
    return {**opt, "env": env, "flags": flags}


def restart(name: str, *, proc=subprocess.run) -> str:
    """Respawn worker `name`'s pane resuming its session, its status line as core config's status_line says now; returns
    the shell string it ran."""
    _check(name)
    opt = _stored(name, proc)
    status_line = _status_line()
    try:
        resume = tui_claude.with_hooks([opt["claude"], "--resume", opt["sid"], "--name", name, *opt["flags"]],
                                       opt["events"])
        tui_claude.decorate(name, opt["events"], status_line=status_line, proc=proc)
    except tui_claude.TuiError as e:
        raise WorkersError(str(e)) from e
    words = ["env", *(w for v in STRIP for w in ("-u", v)), *(f"{k}={v}" for k, v in opt["env"].items()), *resume]
    cmd = " ".join(shlex.quote(w) for w in words)
    _tmux(["tmux", "set-option", "-t", f"={name}:", "@state", ""], proc)
    print(cmd, file=sys.stderr)
    res = _run(proc, ["tmux", "respawn-pane", "-k", "-t", f"={name}:", "-c", opt["cwd"], cmd])
    if res.returncode != 0:
        raise WorkersError((res.stderr or "").strip() or f"tmux respawn-pane exited {res.returncode}")
    return cmd


def reply(name: str, *, proc=subprocess.run, config: str | None = None) -> str:
    """The text blocks of worker `name`'s last text-bearing assistant message, joined by newlines."""
    _check(name)
    sid = _option(name, "@sid", proc)
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", sid):
        raise WorkersError(f"{name}: bad session id {sid!r}")
    config = config or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    found = glob.glob(os.path.join(glob.escape(config), "projects", "*", sid + ".jsonl"))
    if not found:
        raise WorkersError(f"no transcript for {sid}: started with a leaked Claude Code variable?")
    blocks = []
    try:
        with open(found[0], encoding="utf-8", errors="replace") as f:
            for n, raw in enumerate(f):
                try:
                    entry = json.loads(raw)
                    message = entry["message"]
                    content = message["content"]
                    if entry["type"] != "assistant" or not isinstance(content, list):
                        continue
                    texts = [b["text"] for b in content if b["type"] == "text"]
                    mid = message.get("id") or f"line{n}"
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if texts:
                    blocks.append((mid, texts))
    except OSError as e:
        raise WorkersError(f"transcript {found[0]}: {e.strerror}") from e
    if not blocks:
        return ""
    return "\n".join(t for mid, texts in blocks if mid == blocks[-1][0] for t in texts)


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
    p.add_argument("--anchor")
    p.add_argument("--split", choices=("right", "below"))
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            sid = start(a.name, a.events, cwd=a.cwd or os.getcwd(), prompt=a.prompt, flags=flags,
                        env=dict(os.environ), anchor=a.anchor, split=a.split, proc=subprocess.run)
            print(f"{a.name} {sid}")
        elif a.cmd == "restart":
            restart(a.name, proc=subprocess.run)
        else:
            text = reply(a.name, proc=subprocess.run)
            if text:
                print(text)
    except WorkersError as e:
        print(f"workers: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
