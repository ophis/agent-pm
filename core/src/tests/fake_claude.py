"""A stand-in `claude` for tests that run drive.py as a process; install() puts it on PATH. Stdlib only, importing
no core module: it models Claude Code's CLI, never drive.py's flags or a role/task.

claude [-p] [PROMPT] [--session-id SID | --resume SID] [--output-format F] [--verbose] [--settings JSON] ...
Knows only the options src/clients/claude.py and [clients.claude].flags emit: any other is a usage error (exit 2). Exits
1 with a stderr line on: no prompt; both --session-id and --resume; --settings not a JSON object; -p with stream-json
but no --verbose (Claude Code's rule); --resume SID without SID's transcript.

Session: --resume's SID, else --session-id's, else a new one. Its transcript (Claude Code's path rule, restated:
~/.claude/projects/<realpath(cwd), each non-[A-Za-z0-9] char "-">/<sid>.jsonl) gets a user line (the prompt), then an
assistant line per text step; a resume appends.

Scenario: the JSON object in file $FAKE_CLAUDE_SCENARIO (unset, unreadable or any other key: exit 1); a later key is an
explicit extension.
  steps  run in order; default []. A string is assistant text: with --output-format stream-json an assistant line on
         stdout, else a plain line. A dict is a report line in drive_test's builder shapes, {"kind": "progress", "name",
         "text"} or {"kind": "outcome", "outcome": {status, title, summary, questions, url, files, deliverable}}: the
         fake runs it as report.py's subcommand, through the report command, the prompt's first backticked
         `python3 <path>/report.py --to <channel>` (none: exit 1). A failing call's stderr goes to the fake's; it goes
         on. Any other step: exit 1.
  exit   the exit code after the steps; default 0.
  hang   true: after the steps, sleep until killed; default false. It exits by itself once orphaned or after HANG_CAP s.
  log    a file; on start the fake appends {"pid", "argv" (without argv[0]), "cwd"} to it as a JSON line.
stream-json opens with a system init line and, on a normal exit, ends with a result line.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

ENV = "FAKE_CLAUDE_SCENARIO"
HANG_CAP = 120   # seconds
DEFAULTS = {"steps": [], "exit": 0, "hang": False, "log": None}
REPORTS = {"progress": {"kind", "name", "text"}, "outcome": {"kind", "outcome"}}
FIELDS = ("status", "title", "summary", "questions", "url", "files", "deliverable")
REPORT_TIMEOUT = 30   # seconds


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
    if not isinstance(data["steps"], list):
        sys.exit("fake_claude.py: steps: want a list")
    for step in data["steps"]:
        if isinstance(step, str):
            continue
        if not isinstance(step, dict) or set(step) != REPORTS.get(step.get("kind")):
            sys.exit(f"fake_claude.py: bad step {step!r}")
        if step["kind"] == "outcome":
            if not isinstance(step["outcome"], dict):
                sys.exit(f"fake_claude.py: bad step {step!r}")
            if extra := sorted(set(step["outcome"]) - set(FIELDS)):
                sys.exit(f"fake_claude.py: outcome key {extra[0]!r}: report.py can't carry it")
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
    if not a.prompt:
        sys.exit("Error: no prompt")
    if a.session_id and a.resume:
        sys.exit("Error: --session-id and --resume exclude each other")
    if a.settings is not None:
        try:
            settings = json.loads(a.settings)
        except ValueError:
            settings = None
        if not isinstance(settings, dict):
            sys.exit("Error: --settings is no JSON object")
    if a.print and a.output_format == "stream-json" and not a.verbose:
        sys.exit("Error: When using --print, --output-format=stream-json requires --verbose")
    return a


def report_command(prompt: str) -> list[str] | None:
    """The prompt's first backticked `python3 <path>/report.py --to <channel>`, split."""
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
    stream = a.output_format == "stream-json"

    def say(line: dict) -> None:
        print(json.dumps(line), flush=True)

    def keep(role: str, content) -> None:
        with open(transcript, "a") as f:
            f.write(json.dumps({"type": role, "sessionId": sid, "cwd": cwd,
                                "message": {"role": role, "content": content}}) + "\n")

    if stream:
        say({"type": "system", "subtype": "init", "session_id": sid, "cwd": cwd})
    keep("user", a.prompt)
    command = None
    for step in s["steps"]:
        if isinstance(step, str):
            content = [{"type": "text", "text": step}]
            keep("assistant", content)
            if stream:
                say({"type": "assistant", "message": {"role": "assistant", "content": content}, "session_id": sid})
            else:
                print(step, flush=True)
            continue
        command = command or report_command(a.prompt)
        if command is None:
            sys.exit("fake_claude.py: a report step, but the prompt names no report command")
        report(command, step)
    if s["hang"]:
        until = time.monotonic() + HANG_CAP
        while os.getppid() == parent and time.monotonic() < until:
            time.sleep(0.1)
        return 1
    if stream:
        say({"type": "result", "subtype": "success" if s["exit"] == 0 else "error_during_execution",
             "is_error": s["exit"] != 0, "session_id": sid})
    return s["exit"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
