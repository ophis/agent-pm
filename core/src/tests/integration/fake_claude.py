"""A stand-in `claude` for tests that run drive.py as a process, or workers.py and tui_claude.py through tmux; install()
puts it on PATH. Stdlib only, importing no core module: it models Claude Code's CLI and `--settings` hooks, never
drive.py's flags or a role/task.

claude [-p] [PROMPT] [--session-id SID | --resume SID] [--output-format F] [--verbose] [--settings JSON] ...
Knows only the options src/clients/claude.py and [clients.claude].flags emit: any other is a usage error (exit 2). Exits
1 with a stderr line on: -p without a prompt; both --session-id and --resume; --settings not a JSON object; -p with
stream-json but no --verbose (Claude Code's rule); --resume SID without SID's transcript.

Session: --resume's SID, else --session-id's, else a new one. Its transcript (Claude Code's path rule, restated:
~/.claude/projects/<realpath(cwd), each non-[A-Za-z0-9] char "-">/<sid>.jsonl) gets a user line (a turn's prompt), then
an assistant line per text step; a resume appends. No turn, no transcript.

Modes: with -p (print) one turn, the prompt's, then `hang` or exit `exit`. Without -p (interactive) the prompt is
optional: with one, the first turn starts at once; each later turn starts on a line of stdin, its newline stripped. Turn
k runs the k-th of [steps, *turns], none past the end. Stdin EOF or a read error: exit `exit`. Orphaned or HANG_CAP s
without a line: exit 1. `hang` is ignored; text steps are plain lines, never stream-json.
A turn: the UserPromptSubmit hooks, the user transcript line, the steps, the Stop hooks (none if it hangs).

Scenario: the JSON object in file $FAKE_CLAUDE_SCENARIO (unset, unreadable or any other key: exit 1); a later key is an
explicit extension.
  steps  a turn's steps, run in order; default []. A string is assistant text: with -p and --output-format stream-json
         an assistant line on stdout, else a plain line. A dict is a report line in drive_test's builder shapes,
         {"kind": "progress", "name", "text"} or {"kind": "outcome", "outcome": {status, title, summary, questions, url,
         files, deliverable}}: the fake runs it as report.py's subcommand, through the report command, the first
         backticked `python3 <path>/report.py --to <channel>` in the turn prompts so far (none: exit 1). A failing
         call's stderr goes to the fake's; it goes on. {"kind": "hook", "event": E} runs E's hooks now (E a non-empty
         string). {"kind": "write", "path": P, "text": T} writes string T to file P (made or replaced), as the Write
         tool; {"kind": "read", "path": P} is a text step of P's text, as the Read tool; P a non-empty string, relative
         to the cwd. A failing write or read: a stderr line; it goes on. Any other step: exit 1.
  turns  a list of step lists; default []. Interactive mode's second turn on. A non-list or a non-list entry: exit 1.
  exit   the exit code after the turns; default 0.
  hang   true: after the steps, sleep until killed; default false. It exits by itself once orphaned or after HANG_CAP s.
  log    a file; on start the fake appends {"pid", "argv" (without argv[0]), "cwd"} to it as a JSON line.
stream-json opens with a system init line and, on a normal exit, ends with a result line.

Hooks: the `hooks` object of --settings (none: nothing runs). Event E runs each command hook (`"type": "command"`) of
each hooks[E] entry whose `matcher` is absent, "" or "*" (any other: skipped; the fake models no tool or notification
type), in order: /bin/sh -c <command>, in the fake's cwd and env, stdout discarded, stderr the fake's, HOOK_TIMEOUT s.
Stdin: one JSON object {"session_id", "transcript_path", "cwd", "hook_event_name"} plus "prompt" (UserPromptSubmit) or
"stop_hook_active": false (Stop). A hook failing, exiting non-zero or timing out is skipped, as is a malformed entry.
"""
import argparse
import json
import os
import re
import select
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator

ENV = "FAKE_CLAUDE_SCENARIO"
HANG_CAP = 120   # seconds
DEFAULTS = {"steps": [], "turns": [], "exit": 0, "hang": False, "log": None}
REPORTS = {"progress": {"kind", "name", "text"}, "outcome": {"kind", "outcome"}}
FILES = {"write": {"kind", "path", "text"}, "read": {"kind", "path"}}
STEPS = {**REPORTS, **FILES, "hook": {"kind", "event"}}
FIELDS = ("status", "title", "summary", "questions", "url", "files", "deliverable")
REPORT_TIMEOUT = 30   # seconds
HOOK_TIMEOUT = 60   # seconds


def install(bin_dir: str, tail: str = "/usr/bin:/bin") -> str:
    """Writes the shims a run needs on PATH into `bin_dir`, each exec'ing (the pid drive kills is the program's):
    `claude`, this fake; `python3`, sys.executable, as the prompt's report command starts with `python3` and report.py
    needs Python >= 3.10. A later shim goes here too. Returns the PATH "<bin_dir>:<tail>"."""
    q = shlex.quote
    shims = {"claude": f"{q(sys.executable)} {q(os.path.abspath(__file__))}", "python3": q(sys.executable)}
    for name, cmd in shims.items():
        path = os.path.join(bin_dir, name)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nexec {cmd} "$@"\n')
        os.chmod(path, 0o755)
    return f"{bin_dir}:{tail}"


def check(where: str, steps) -> None:
    """Exits 1 unless `steps` is a list of valid steps."""
    if not isinstance(steps, list):
        sys.exit(f"fake_claude.py: {where}: want a list")
    for step in steps:
        if isinstance(step, str):
            continue
        kind = step.get("kind") if isinstance(step, dict) else None
        if not isinstance(kind, str) or set(step) != STEPS.get(kind):
            sys.exit(f"fake_claude.py: bad step {step!r}")
        if kind == "outcome":
            if not isinstance(step["outcome"], dict):
                sys.exit(f"fake_claude.py: bad step {step!r}")
            if extra := sorted(set(step["outcome"]) - set(FIELDS)):
                sys.exit(f"fake_claude.py: outcome key {extra[0]!r}: report.py can't carry it")
        if kind == "hook" and not (isinstance(step["event"], str) and step["event"]):
            sys.exit(f"fake_claude.py: bad step {step!r}")
        if kind in FILES and not (isinstance(step["path"], str) and step["path"]
                                  and isinstance(step.get("text", ""), str)):
            sys.exit(f"fake_claude.py: bad step {step!r}")


def scenario() -> dict:
    path = os.environ.get(ENV)
    if not path:
        sys.exit(f"fake_claude.py: {ENV} is unset")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"fake_claude.py: {ENV}: {e}")
    if not isinstance(data, dict):
        sys.exit("fake_claude.py: the scenario is no JSON object")
    if extra := sorted(set(data) - set(DEFAULTS)):
        sys.exit(f"fake_claude.py: unknown scenario key {extra[0]!r}")
    data = {**DEFAULTS, **data}
    if not isinstance(data["turns"], list):
        sys.exit("fake_claude.py: turns: want a list")
    for where, steps in [("steps", data["steps"]), *((f"turns[{i}]", t) for i, t in enumerate(data["turns"]))]:
        check(where, steps)
    return data


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="claude", allow_abbrev=False)
    for flag in ("--verbose", "--strict-mcp-config"):
        ap.add_argument(flag, action="store_true")
    ap.add_argument("-p", "--print", action="store_true")
    for opt in ("--session-id", "--resume", "--model", "--effort", "--permission-mode", "--setting-sources",
                "--output-format", "--settings", "--mcp-config", "--name"):
        ap.add_argument(opt)
    for opt in ("--add-dir", "--allowedTools"):
        ap.add_argument(opt, nargs="+", action="extend")
    ap.add_argument("prompt", nargs="?")
    a = ap.parse_args(argv)
    if a.print and not a.prompt:
        sys.exit("Error: no prompt")
    if a.session_id and a.resume:
        sys.exit("Error: --session-id and --resume exclude each other")
    a.hooks = None
    if a.settings is not None:
        try:
            settings = json.loads(a.settings)
        except ValueError:
            settings = None
        if not isinstance(settings, dict):
            sys.exit("Error: --settings is no JSON object")
        a.hooks = settings.get("hooks")
    if a.print and a.output_format == "stream-json" and not a.verbose:
        sys.exit("Error: When using --print, --output-format=stream-json requires --verbose")
    return a


def report_command(prompts: list[str]) -> list[str] | None:
    """The first backticked `python3 <path>/report.py --to <channel>` in `prompts`, split."""
    for prompt in prompts:
        for span in re.findall(r"`([^`]*)`", prompt):
            try:
                words = shlex.split(span)
            except ValueError:
                continue
            if (len(words) == 4 and words[0] == "python3" and os.path.basename(words[1]) == "report.py"
                    and words[2] == "--to"):
                return words
    return None


def report(command: list[str], step: dict) -> None:
    """Runs report.py's subcommand for `step`, its stdout kept off drive's pipe."""
    deliverable = None
    try:
        if step["kind"] == "progress":
            sub = ["progress", step["name"], step["text"]]
        else:
            o = step["outcome"]
            sub = ["outcome", *(f"--{k}={o[k]}" for k in ("status", "title", "summary") if k in o),
                   *(f"--question={q}" for q in o.get("questions", [])), *([f"--url={o['url']}"] if "url" in o else []),
                   *(f"--file={p}" for p in o.get("files", []))]
            if "deliverable" in o:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
                    deliverable = f.name
                    f.write(o["deliverable"])
                sub.append(f"--deliverable={deliverable}")
        res = subprocess.run(command + sub, capture_output=True, text=True, timeout=REPORT_TIMEOUT,
                             stdin=subprocess.DEVNULL)
        if res.returncode:
            print(res.stderr, end="", file=sys.stderr, flush=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"fake_claude.py: report.py: {e}", file=sys.stderr, flush=True)
    finally:
        if deliverable:
            os.unlink(deliverable)


def file_step(step: dict) -> str | None:
    """Runs a write or read step; returns a read's text, else None."""
    try:
        if step["kind"] == "write":
            with open(step["path"], "w", encoding="utf-8") as f:
                f.write(step["text"])
            return None
        with open(step["path"], encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError) as e:
        print(f"fake_claude.py: {step['kind']}: {e}", file=sys.stderr, flush=True)
        return None


def run_hooks(hooks, event: str, payload: dict) -> None:
    """Runs `event`'s command hooks from the --settings `hooks` object, `payload` as JSON on their stdin."""
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        items = entry.get("hooks") if isinstance(entry, dict) and entry.get("matcher", "") in ("", "*") else None
        for hook in items if isinstance(items, list) else []:
            if isinstance(hook, dict) and hook.get("type") == "command" and isinstance(hook.get("command"), str):
                try:
                    subprocess.run(["/bin/sh", "-c", hook["command"]], input=json.dumps(payload), text=True,
                                   stdout=subprocess.DEVNULL, timeout=HOOK_TIMEOUT)
                except (OSError, subprocess.SubprocessError) as e:
                    print(f"fake_claude.py: {event} hook: {e}", file=sys.stderr, flush=True)


class Stranded(Exception):
    """Orphaned, or HANG_CAP s without a line."""


def inputs(prompt: str | None, parent: int) -> Iterator[str]:
    """`prompt` if any, then stdin's lines without their newline (an unterminated last one too), until EOF or a read
    error. Waits in short slices to notice orphaning; raises Stranded."""
    if prompt:
        yield prompt
    # os.read, not sys.stdin: select can't see what a buffered reader already holds.
    fd, buf = sys.stdin.fileno(), b""
    while True:
        until = time.monotonic() + HANG_CAP
        while b"\n" not in buf:
            if os.getppid() != parent or time.monotonic() > until:
                raise Stranded
            try:
                if not select.select([fd], [], [], 0.1)[0]:
                    continue
                chunk = os.read(fd, 65536)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                if buf:
                    yield buf.decode(errors="replace")
                return
            buf += chunk
        line, _, buf = buf.partition(b"\n")
        yield line.decode(errors="replace")


def main(argv: list[str]) -> int:
    parent = os.getppid()
    s = scenario()
    if s["log"]:
        with open(s["log"], "a") as f:
            f.write(json.dumps({"pid": os.getpid(), "argv": argv, "cwd": os.getcwd()}) + "\n")
    a = parse(argv)
    sid, cwd = a.resume or a.session_id or str(uuid.uuid4()), os.getcwd()
    project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    transcript = os.path.join(os.path.expanduser("~/.claude/projects"), project, f"{sid}.jsonl")
    if a.resume and not os.path.isfile(transcript):
        sys.exit(f"No conversation found with session ID: {sid}")
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    stream = a.print and a.output_format == "stream-json"

    def say(line: dict) -> None:
        print(json.dumps(line), flush=True)

    def keep(role: str, content) -> None:
        with open(transcript, "a") as f:
            f.write(json.dumps({"type": role, "sessionId": sid, "cwd": cwd,
                                "message": {"role": role, "content": content}}) + "\n")

    def hook(event: str, **extra) -> None:
        run_hooks(a.hooks, event, {"session_id": sid, "transcript_path": transcript, "cwd": cwd,
                                   "hook_event_name": event, **extra})

    prompts, command = [], None

    def speak(text: str) -> None:
        content = [{"type": "text", "text": text}]
        keep("assistant", content)
        if stream:
            say({"type": "assistant", "message": {"role": "assistant", "content": content}, "session_id": sid})
        else:
            print(text, flush=True)

    def turn(prompt: str, steps: list, stop: bool = True) -> None:
        nonlocal command
        prompts.append(prompt)
        hook("UserPromptSubmit", prompt=prompt)
        keep("user", prompt)
        for step in steps:
            if isinstance(step, str):
                speak(step)
            elif step["kind"] == "hook":
                hook(step["event"])
            elif step["kind"] in FILES:
                if (text := file_step(step)) is not None:
                    speak(text)
            else:
                command = command or report_command(prompts)
                if command is None:
                    sys.exit("fake_claude.py: a report step, but no prompt so far names a report command")
                report(command, step)
        if stop:
            hook("Stop", stop_hook_active=False)

    if a.print:
        if stream:
            say({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd})
        turn(a.prompt, s["steps"], stop=not s["hang"])
        if s["hang"]:
            until = time.monotonic() + HANG_CAP
            while os.getppid() == parent and time.monotonic() < until:
                time.sleep(0.1)
            return 1
        if stream:
            say({"type": "result", "subtype": "success" if s["exit"] == 0 else "error_during_execution",
                 "is_error": s["exit"] != 0, "session_id": sid})
        return s["exit"]
    turns = [s["steps"], *s["turns"]]
    try:
        for k, prompt in enumerate(inputs(a.prompt, parent)):
            turn(prompt, turns[k] if k < len(turns) else [])
    except Stranded:
        return 1
    return s["exit"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
