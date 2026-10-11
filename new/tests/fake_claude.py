"""A stand-in `claude` for the new core's integration tests, print mode only; install() puts it on PATH. Stdlib only,
importing no core module: it models Claude Code's CLI, never the adapter.

claude -p [PROMPT] [--session-id SID | --resume SID] --output-format stream-json --verbose ... [--] [PROMPT]
Knows only the options src/adapters/claude/ (with core/config.toml's flags) and its usage probe emit: any other, no -p
or another output format is a usage error (exit 2). Exits 1 with a stderr line on: -p without a prompt; both
--session-id and --resume; --settings not a JSON object; stream-json without --verbose (Claude Code's rule); --resume
SID without SID's transcript.

Session: --resume's SID, else --session-id's, else a new uuid4. Its transcript (Claude Code's path rule, restated:
<$CLAUDE_CONFIG_DIR, else ~/.claude>/projects/<realpath(cwd), each non-[A-Za-z0-9] char "-">/<sid>.jsonl) gets a user
line (the prompt), then an assistant line per step; a resume appends.

Stdout, one JSON line each: system init; the `rate_limit` event, if any; each step's lines; unless it hangs, a result
(exit 0: success, `result` the last text, "" none; else error_during_execution).

Scenario: the JSON object in file $FAKE_CLAUDE_SCENARIO (unset, unreadable, a bad value or any other key: exit 1).
  steps       run in order; default []. A string: assistant text. {"kind": "tool", "name": N}: an assistant tool_use of
              tool N. {"kind": "limit", "resets_at": T}: a rejected rate_limit_event (resetsAt T; null: none), then an
              assistant message with error "rate_limit" and text LIMIT. {"kind": "write", "path": P, "text": T}:
              writes T to file P (relative to the cwd; parent dirs made), printing nothing.
  resume      steps run instead of `steps` on --resume; default none (`steps`).
  rate_limit  a rate_limit_info object, printed as a rate_limit_event after init; default none.
  exit        the exit code; default 0.
  hang        true: after the steps, sleep until killed; it exits 1 by itself once orphaned or after HANG_CAP s.
  log         a file; on start the fake appends {"pid", "argv" (without argv[0]), "cwd"} to it as a JSON line.
"""
import argparse
import json
import os
import re
import shlex
import sys
import time
import uuid

ENV = "FAKE_CLAUDE_SCENARIO"
HANG_CAP = 120   # seconds
DEFAULTS = {"steps": [], "resume": None, "rate_limit": None, "exit": 0, "hang": False, "log": None}
LIMIT = "You've hit your usage limit."


def install(bin_dir: str, tail: str = "/usr/bin:/bin") -> str:
    """Writes the `claude` shim into `bin_dir`, exec'ing this fake (so the pid the Driver starts is the fake's).
    Returns the PATH "<bin_dir>:<tail>"."""
    q = shlex.quote
    path = os.path.join(bin_dir, "claude")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\nexec {q(sys.executable)} -I {q(os.path.abspath(__file__))} "$@"\n')
    os.chmod(path, 0o755)
    return f"{bin_dir}:{tail}"


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _step(step) -> bool:
    if isinstance(step, str):
        return True
    kind = step.get("kind") if isinstance(step, dict) else None
    if kind == "tool":
        return set(step) == {"kind", "name"} and isinstance(step["name"], str)
    if kind == "write":
        return (set(step) == {"kind", "path", "text"} and isinstance(step["path"], str) and step["path"] != ""
                and isinstance(step["text"], str))
    return kind == "limit" and set(step) == {"kind", "resets_at"} and (
        step["resets_at"] is None or _number(step["resets_at"]))


def _steps(value) -> bool:
    return isinstance(value, list) and all(map(_step, value))


CHECKS = {"steps": _steps,
          "resume": lambda v: v is None or _steps(v),
          "rate_limit": lambda v: v is None or isinstance(v, dict),
          "exit": lambda v: isinstance(v, int) and not isinstance(v, bool),
          "hang": lambda v: isinstance(v, bool),
          "log": lambda v: v is None or isinstance(v, str)}


def scenario() -> dict:
    path = os.environ.get(ENV)
    if not path:
        sys.exit(f"fake_claude.py: {ENV} is unset")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"fake_claude.py: {ENV}: {e}")
    if not isinstance(data, dict):
        sys.exit("fake_claude.py: the scenario is no JSON object")
    if extra := sorted(set(data) - set(DEFAULTS)):
        sys.exit(f"fake_claude.py: unknown scenario key {extra[0]!r}")
    data = {**DEFAULTS, **data}
    if bad := [key for key, ok in CHECKS.items() if not ok(data[key])]:
        sys.exit(f"fake_claude.py: bad scenario {bad[0]}")
    return data


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="claude", allow_abbrev=False)
    for flag in ("--verbose", "--strict-mcp-config"):
        ap.add_argument(flag, action="store_true")
    ap.add_argument("-p", "--print", action="store_true")
    for opt in ("--session-id", "--resume", "--model", "--effort", "--permission-mode", "--setting-sources",
                "--output-format", "--settings", "--mcp-config"):
        ap.add_argument(opt)
    for opt in ("--add-dir", "--allowedTools"):
        ap.add_argument(opt, nargs="+", action="extend")
    ap.add_argument("prompt", nargs="?")
    a = ap.parse_args(argv)
    if not a.print or a.output_format != "stream-json":
        ap.error("this fake models only -p with --output-format stream-json")
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
    if not a.verbose:
        sys.exit("Error: When using --print, --output-format=stream-json requires --verbose")
    return a


def main(argv: list[str]) -> int:
    parent = os.getppid()
    s = scenario()
    if s["log"]:
        with open(s["log"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "argv": argv, "cwd": os.getcwd()}) + "\n")
    a = parse(argv)
    sid, cwd = a.resume or a.session_id or str(uuid.uuid4()), os.getcwd()
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    transcript = os.path.join(config, "projects", project, f"{sid}.jsonl")
    if a.resume and not os.path.isfile(transcript):
        sys.exit(f"No conversation found with session ID: {sid}")
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    last = ""

    def say(record: dict) -> None:
        print(json.dumps({**record, "session_id": sid}), flush=True)

    def keep(role: str, content) -> None:
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": role, "sessionId": sid, "cwd": cwd,
                                "message": {"role": role, "content": content}}) + "\n")

    def assistant(content: list, **extra) -> None:
        keep("assistant", content)
        say({"type": "assistant", "message": {"role": "assistant", "content": content}, **extra})

    say({"type": "system", "subtype": "init", "cwd": cwd})
    if s["rate_limit"] is not None:
        say({"type": "rate_limit_event", "rate_limit_info": s["rate_limit"]})
    keep("user", a.prompt)
    steps = s["resume"] if a.resume and s["resume"] is not None else s["steps"]
    for k, step in enumerate(steps):
        if isinstance(step, str):
            last = step
            assistant([{"type": "text", "text": step}])
        elif step["kind"] == "tool":
            assistant([{"type": "tool_use", "id": f"toolu_{k}", "name": step["name"], "input": {}}])
        elif step["kind"] == "write":
            os.makedirs(os.path.dirname(os.path.abspath(step["path"])), exist_ok=True)
            with open(step["path"], "w", encoding="utf-8") as f:
                f.write(step["text"])
        else:
            resets = {} if step["resets_at"] is None else {"resetsAt": step["resets_at"]}
            say({"type": "rate_limit_event",
                 "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour", **resets}})
            last = LIMIT
            assistant([{"type": "text", "text": LIMIT}], error="rate_limit")
    if s["hang"]:
        until = time.monotonic() + HANG_CAP
        while os.getppid() == parent and time.monotonic() < until:
            time.sleep(0.1)
        return 1
    ok = s["exit"] == 0
    say({"type": "result", "subtype": "success" if ok else "error_during_execution", "is_error": not ok,
         **({"result": last} if ok else {})})
    return s["exit"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
