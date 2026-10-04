"""Starts and directs Claude Code workers in tmux panes through core/src/tui.py. Stdlib only."""
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


def _touch(events: str) -> None:
    try:
        os.close(os.open(events, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600))
    except OSError as e:
        raise WorkersError(f"events file {events}: {e.strerror}") from e


def _beside(events: str, proc) -> list:
    """tui.py layout flags: below the newest attached worker sharing the events file, else none."""
    res = proc(["tmux", "list-sessions", "-F", SESSIONS], **RUN)
    best = None
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 4 or fields[2] != events:
                continue
            try:
                attached, started = int(fields[1]), int(fields[3])
            except ValueError:
                continue
            if attached > 0 and (best is None or started > best[0]):
                best = (started, fields[0])
    return ["--beside", best[1], "--split", "below"] if best else []


def start(name: str, events: str, *, cwd: str, prompt: str | None = None, flags=(), env: dict,
          proc=subprocess.run) -> str:
    """Start worker `name` beside its siblings, record its options on the tmux session; returns its session id."""
    if not NAME.fullmatch(name):
        raise WorkersError(f"bad name {name!r}: use [A-Za-z0-9_-]+")
    events, cwd = os.path.abspath(events), os.path.abspath(cwd)
    claude = shutil.which("claude", path=env.get("PATH"))
    if claude is None:
        raise WorkersError("claude not found on PATH")
    claude = os.path.abspath(claude)
    _touch(events)
    sid = str(uuid.uuid4())
    clean = {k: v for k, v in env.items() if k not in STRIP}
    argv = [sys.executable, TUI, "start", name, *_beside(events, proc), "--", claude, "--session-id", sid,
            "--name", name, "--settings", hooks(events), *flags, *([prompt] if prompt else [])]
    res = proc(argv, cwd=cwd, env=clean, **RUN)
    if res.returncode != 0:
        raise WorkersError((res.stderr or "").strip() or f"tui.py exited {res.returncode}")
    options = {"@sid": sid, "@cwd": cwd, "@events": events, "@claude": claude,
               "@env": json.dumps({k: clean[k] for k in ENV_KEYS if k in clean}),
               "@flags": json.dumps(list(flags)), "@started": str(time.time_ns())}
    for key, value in options.items():
        res = proc(["tmux", "set-option", "-t", f"={name}:", key, value], **RUN)
        if res.returncode != 0:
            raise WorkersError(f"tmux set-option {key}: {(res.stderr or '').strip()}")
    return sid


def _option(name: str, key: str, proc) -> str:
    res = proc(["tmux", "show-options", "-t", f"={name}:", "-v", key], **RUN)
    if res.returncode != 0:
        raise WorkersError(f"{name}: not a worker")
    return res.stdout.rstrip("\n")


def restart(name: str, *, proc=subprocess.run) -> str:
    """Respawn worker `name`'s pane resuming its session; returns the shell string it ran."""
    if not NAME.fullmatch(name):
        raise WorkersError(f"bad name {name!r}: use [A-Za-z0-9_-]+")
    opt = {k: _option(name, "@" + k, proc) for k in ("sid", "cwd", "events", "claude", "env", "flags")}
    words = ["env", *(w for v in STRIP for w in ("-u", v)),
             *(f"{k}={v}" for k, v in json.loads(opt["env"]).items()),
             opt["claude"], "--resume", opt["sid"], "--name", name, "--settings", hooks(opt["events"]),
             *json.loads(opt["flags"])]
    cmd = " ".join(shlex.quote(w) for w in words)
    print(cmd, file=sys.stderr)
    res = proc(["tmux", "respawn-pane", "-k", "-t", f"={name}:", "-c", opt["cwd"], cmd], **RUN)
    if res.returncode != 0:
        raise WorkersError((res.stderr or "").strip() or f"tmux respawn-pane exited {res.returncode}")
    return cmd


def reply(name: str, *, proc=subprocess.run, config: str | None = None) -> str:
    """The text blocks of worker `name`'s last assistant message that has any, joined by newlines."""
    sid = _option(name, "@sid", proc)
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", sid):
        raise WorkersError(f"{name}: bad session id {sid!r}")
    config = config or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    found = glob.glob(os.path.join(glob.escape(config), "projects", "*", sid + ".jsonl"))
    if not found:
        raise WorkersError(f"no transcript for {sid}: started with a leaked Claude Code variable?")
    last = ""
    with open(found[0], encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                entry = json.loads(raw)
                content = entry["message"]["content"]
                if entry["type"] != "assistant" or not isinstance(content, list):
                    continue
                texts = [b["text"] for b in content if b["type"] == "text"]
            except (ValueError, KeyError, TypeError):
                continue
            if texts:
                last = "\n".join(texts)
    return last


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
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            sid = start(a.name, a.events, cwd=a.cwd or os.getcwd(), prompt=a.prompt, flags=flags,
                        env=dict(os.environ), proc=subprocess.run)
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
