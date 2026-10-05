"""Starts and directs Claude Code workers in tmux panes through core/src/tui.py. Stdlib only."""
from __future__ import annotations

import argparse
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

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
TUI = os.path.join(ROOT, "core", "src", "tui.py")
NAME = re.compile(r"[A-Za-z0-9_-]+")
STRIP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
         "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_EFFORT")
ENV_KEYS = ("PATH", "CLAUDE_CONFIG_DIR")
MATCHER = "permission_prompt|elicitation_dialog|agent_needs_input"
SESSIONS = "#{session_name}\t#{session_attached}\t#{@events}\t#{@started}"
RUN = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}


class WorkersError(Exception):
    pass


def hooks(events: str) -> str:
    """The --settings JSON: Stop appends `done`, a blocking Notification `blocked`, to the events file."""
    def command(word: str) -> str:
        return ('[ -n "$TMUX_PANE" ] && echo "$(date +%H:%M:%S) $(tmux display -p -t "$TMUX_PANE" \'#S\') '
                f'{word}" >> {shlex.quote(events)} || true')

    def entry(word: str) -> list:
        return [{"type": "command", "command": command(word)}]

    return json.dumps({"hooks": {
        "Stop": [{"hooks": entry("done")}],
        "Notification": [{"matcher": MATCHER, "hooks": entry("blocked")}]}})


def _run(proc, argv, **kw):
    try:
        return proc(argv, **{**RUN, **kw})
    except OSError as e:
        raise WorkersError(f"{argv[0]}: {e.strerror or e}") from e


def _check(name: str) -> None:
    if not NAME.fullmatch(name):
        raise WorkersError(f"bad name {name!r}: use [A-Za-z0-9_-]+")


def _touch(events: str) -> None:
    try:
        fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise WorkersError(f"events file {events}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise WorkersError(f"events file {events}: not a regular file owned by you")


def _beside(name: str, events: str, proc) -> list:
    """tui.py layout flags: below the newest attached other worker on the events file, else none."""
    res = _run(proc, ["tmux", "list-sessions", "-F", SESSIONS])
    best = None
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 4 or fields[2] != events or fields[0] == name:
                continue
            try:
                attached, started = int(fields[1]), int(fields[3])
            except ValueError:
                continue
            if attached > 0 and (best is None or started > best[0]):
                best = (started, fields[0])
    return ["--beside", best[1], "--split", "below"] if best else []


def start(name: str, events: str, *, cwd: str, prompt: str | None = None, flags=(), env: dict,
          beside: str | None = None, split: str | None = None, proc=subprocess.run) -> str:
    """Start worker `name`, record its options on the tmux session; returns the session id.
    `beside` or `split` replaces the automatic placement; tui.py defaults the other."""
    _check(name)
    if beside is not None:
        _check(beside)
    events, cwd = os.path.abspath(events), os.path.abspath(cwd)
    if not os.path.isdir(cwd):
        raise WorkersError(f"--cwd {cwd}: not a directory")
    claude = shutil.which("claude", path=env.get("PATH"))
    if claude is None:
        raise WorkersError("claude not found on PATH")
    claude = os.path.abspath(claude)
    _touch(events)
    sid = str(uuid.uuid4())
    child_env = {k: v for k, v in env.items() if k not in STRIP}
    layout = [*(["--beside", beside] if beside else []), *(["--split", split] if split else [])]
    argv = [sys.executable, TUI, "start", name, *(layout or _beside(name, events, proc)), "--", claude,
            "--session-id", sid, "--name", name, "--settings", hooks(events), *flags,
            *(["--", prompt] if prompt else [])]
    res = _run(proc, argv, cwd=cwd, env=child_env)
    if res.returncode != 0:
        raise WorkersError((res.stderr or "").strip() or f"tui.py exited {res.returncode}")
    for line in (res.stderr or "").splitlines():
        if line.startswith("tui: show:"):
            print(line, file=sys.stderr)
    options = {"@sid": sid, "@cwd": cwd, "@events": events, "@claude": claude,
               "@env": json.dumps({k: child_env[k] for k in ENV_KEYS if k in child_env}),
               "@flags": json.dumps(list(flags)), "@started": str(time.time_ns())}
    for key, value in options.items():
        try:
            res = _run(proc, ["tmux", "set-option", "-t", f"={name}:", key, value])
            failure = None if res.returncode == 0 else (res.stderr or "").strip()
        except WorkersError as e:
            failure = str(e)
        if failure is not None:
            try:
                _run(proc, ["tmux", "kill-session", "-t", f"={name}"])
            except WorkersError:
                pass
            raise WorkersError(f"tmux set-option {key}: {failure}; start undone")
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
    """Respawn worker `name`'s pane resuming its session; returns the shell string it ran."""
    _check(name)
    opt = _stored(name, proc)
    words = ["env", *(w for v in STRIP for w in ("-u", v)), *(f"{k}={v}" for k, v in opt["env"].items()),
             opt["claude"], "--resume", opt["sid"], "--name", name, "--settings", hooks(opt["events"]),
             *opt["flags"]]
    cmd = " ".join(shlex.quote(w) for w in words)
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
    p.add_argument("--beside")
    p.add_argument("--split", choices=("right", "below"))
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            sid = start(a.name, a.events, cwd=a.cwd or os.getcwd(), prompt=a.prompt, flags=flags,
                        env=dict(os.environ), beside=a.beside, split=a.split, proc=subprocess.run)
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
