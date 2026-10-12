"""Scenario: the JSON object in file $FAKE_CLAUDE_SCENARIO (unset, unreadable or any other key: exit 1); a later key
is an explicit extension.
  steps  a turn's steps, run in order; default []. A string is assistant text: with -p and --output-format stream-json
         an assistant line on stdout, else a plain line. A dict is a report line in drive_test's builder shapes,
         {"kind": "progress", "name", "text"} or {"kind": "outcome", "outcome": {status, title, summary, questions, url,
         files, deliverable}}: the fake runs it as report.py's subcommand, through the report command: the `report` line
         of the first `# Parameters` section in the turn prompts so far that has one (a line `- `<name>`: <inline
         code>`, as compose writes it), `<scripts>` and `<Workdir>` replaced by their lines' values, split into words
         (none: exit 1). A failing call's stderr goes to the fake's; it goes on. {"kind": "hook", "event": E} runs E's
         hooks now (E a non-empty string). {"kind": "write", "path": P, "text": T} writes string T to file P (made or
         replaced), as the Write tool; {"kind": "read", "path": P} is a text step of P's text, as the Read tool; P a
         non-empty string, relative to the cwd. A failing write or read: a stderr line; it goes on. Any other step:
         exit 1.
  turns  a list of step lists; default []. Interactive mode's second turn on. A non-list or a non-list entry: exit 1.
  exit   the exit code after the turns; default 0.
  hang   true: after the steps, sleep until killed; default false. It exits by itself once orphaned or after HANG_CAP s.
  log    a file; on start the fake appends {"pid", "argv" (without argv[0]), "cwd"} to it as a JSON line.
  gate   a file; default none. After the log line and before the first turn (either mode; before any transcript, hook
         or output) the fake polls every 0.05 s until the file exists. Orphaned or HANG_CAP s without it: exit 1 with a
         stderr line. A non-string: exit 1.
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
HANG_CAP = 120
DEFAULTS = {"steps": [], "turns": [], "exit": 0, "hang": False, "log": None, "gate": None}
REPORTS = {"progress": {"kind", "name", "text"}, "outcome": {"kind", "outcome"}}
FILES = {"write": {"kind", "path", "text"}, "read": {"kind", "path"}}
STEPS = {**REPORTS, **FILES, "hook": {"kind", "event"}}
FIELDS = ("status", "title", "summary", "questions", "url", "files", "deliverable")
REPORT_TIMEOUT = 30
HOOK_TIMEOUT = 60
SECTION = re.compile(r"^# Parameters\n(.*?)(?=^# |\Z)", re.M | re.S)
LINE = re.compile(r"^- `([^`\n]+)`: (`+)(.+)\2$", re.M)


def install(bin_dir: str, tail: str = "/usr/bin:/bin") -> str:
    # Each shim exec's (the pid drive kills is the program's); python3 is sys.executable, as the prompt's report
    # command starts with `python3` and report.py needs Python >= 3.10.
    q = shlex.quote
    shims = {
        "claude": f"{q(sys.executable)} {q(os.path.abspath(__file__))}",
        "python3": q(sys.executable),
    }
    for name, cmd in shims.items():
        path = os.path.join(bin_dir, name)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nexec {cmd} "$@"\n')
        os.chmod(path, 0o755)
    return f"{bin_dir}:{tail}"


def check(where: str, steps) -> None:
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
        if kind in FILES and not (
            isinstance(step["path"], str) and step["path"] and isinstance(step.get("text", ""), str)
        ):
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
    if not (data["gate"] is None or isinstance(data["gate"], str)):
        sys.exit("fake_claude.py: gate: want a file path")
    if not isinstance(data["turns"], list):
        sys.exit("fake_claude.py: turns: want a list")
    for where, steps in [
        ("steps", data["steps"]),
        *((f"turns[{i}]", t) for i, t in enumerate(data["turns"])),
    ]:
        check(where, steps)
    return data


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="claude", allow_abbrev=False)
    for flag in ("--verbose", "--strict-mcp-config"):
        ap.add_argument(flag, action="store_true")
    ap.add_argument("-p", "--print", action="store_true")
    for opt in (
        "--session-id",
        "--resume",
        "--model",
        "--effort",
        "--permission-mode",
        "--setting-sources",
        "--output-format",
        "--settings",
        "--mcp-config",
        "--name",
    ):
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


def parameters(prompt: str) -> dict[str, str]:
    section = SECTION.search(prompt)
    # compose._code pads a value holding a backtick with a space each side.
    return (
        {
            name: value[1:-1] if "`" in value else value
            for name, _, value in LINE.findall(section.group(1))
        }
        if section
        else {}
    )


def report_command(prompts: list[str]) -> list[str] | None:
    for prompt in prompts:
        names = parameters(prompt)
        if "report" in names:
            command = names["report"]
            for name in ("<scripts>", "<Workdir>"):
                command = command.replace(name, names.get(name, name))
            try:
                return shlex.split(command)
            except ValueError:
                continue
    return None


def report(command: list[str], step: dict) -> None:
    deliverable = None
    try:
        if step["kind"] == "progress":
            sub = ["progress", step["name"], step["text"]]
        else:
            o = step["outcome"]
            sub = [
                "outcome",
                *(f"--{k}={o[k]}" for k in ("status", "title", "summary") if k in o),
                *(f"--question={q}" for q in o.get("questions", [])),
                *([f"--url={o['url']}"] if "url" in o else []),
                *(f"--file={p}" for p in o.get("files", [])),
            ]
            if "deliverable" in o:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", suffix=".md", delete=False
                ) as f:
                    deliverable = f.name
                    f.write(o["deliverable"])
                sub.append(f"--deliverable={deliverable}")
        res = subprocess.run(
            command + sub,
            capture_output=True,
            text=True,
            timeout=REPORT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if res.returncode:
            print(res.stderr, end="", file=sys.stderr, flush=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"fake_claude.py: report.py: {e}", file=sys.stderr, flush=True)
    finally:
        if deliverable:
            os.unlink(deliverable)


def file_step(step: dict) -> str | None:
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
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        items = (
            entry.get("hooks")
            if isinstance(entry, dict) and entry.get("matcher", "") in ("", "*")
            else None
        )
        for hook in items if isinstance(items, list) else []:
            if (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and isinstance(hook.get("command"), str)
            ):
                try:
                    subprocess.run(
                        ["/bin/sh", "-c", hook["command"]],
                        input=json.dumps(payload),
                        text=True,
                        stdout=subprocess.DEVNULL,
                        timeout=HOOK_TIMEOUT,
                    )
                except (OSError, subprocess.SubprocessError) as e:
                    print(f"fake_claude.py: {event} hook: {e}", file=sys.stderr, flush=True)


class Stranded(Exception):
    pass


def held(path: str, parent: int) -> bool:
    until = time.monotonic() + HANG_CAP
    while not os.path.exists(path):
        if os.getppid() != parent or time.monotonic() > until:
            print(f"fake_claude.py: gate {path}: never created", file=sys.stderr, flush=True)
            return False
        time.sleep(0.05)
    return True


def inputs(prompt: str | None, parent: int) -> Iterator[str]:
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
    if s["gate"] and not held(s["gate"], parent):
        return 1
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    stream = a.print and a.output_format == "stream-json"

    def say(line: dict) -> None:
        print(json.dumps(line), flush=True)

    def keep(role: str, content) -> None:
        with open(transcript, "a") as f:
            f.write(
                json.dumps(
                    {
                        "type": role,
                        "sessionId": sid,
                        "cwd": cwd,
                        "message": {"role": role, "content": content},
                    }
                )
                + "\n"
            )

    def hook(event: str, **extra) -> None:
        run_hooks(
            a.hooks,
            event,
            {
                "session_id": sid,
                "transcript_path": transcript,
                "cwd": cwd,
                "hook_event_name": event,
                **extra,
            },
        )

    prompts, command = [], None

    def speak(text: str) -> None:
        content = [{"type": "text", "text": text}]
        keep("assistant", content)
        if stream:
            say(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": content},
                    "session_id": sid,
                }
            )
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
                    sys.exit(
                        "fake_claude.py: a report step, but no prompt so far names a report command"
                    )
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
            say(
                {
                    "type": "result",
                    "subtype": "success" if s["exit"] == 0 else "error_during_execution",
                    "is_error": s["exit"] != 0,
                    "session_id": sid,
                }
            )
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
