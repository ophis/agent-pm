"""A stand-in `claude` for the new core's integration tests, in print and interactive mode; install() puts it on PATH.
Stdlib only, importing no core module: it models Claude Code's CLI, never the adapter.

claude -p [PROMPT] [--session-id SID | --resume SID] --output-format stream-json --verbose ... [--] [PROMPT]
claude [--session-id SID | --resume SID] ... [--] [PROMPT]   (interactive mode)
Knows only the options src/adapters/claude/ (with core/config.toml's flags) and its usage probe emit: any other, -p
without --output-format stream-json, or --output-format without -p is a usage error (exit 2). Exits 1 with a stderr line
on: -p without a prompt; both --session-id and --resume; --settings not a JSON object; stream-json without --verbose
(Claude Code's rule); --resume SID without SID's transcript.

Session: --resume's SID, else --session-id's, else a new uuid4. Its transcript (Claude Code's path rule, restated:
<$CLAUDE_CONFIG_DIR, else ~/.claude>/projects/<realpath(cwd), each non-[A-Za-z0-9] char "-">/<sid>.jsonl) gets per
turn a user line (the prompt or stdin line), then an assistant line per text or tool step; a resume appends.

Print mode stdout, one JSON line each: system init; the `rate_limit` event, if any; each step's lines; unless it hangs,
a result (exit 0: success, `result` the last text, "" none; else error_during_execution). It runs no hooks.

Interactive mode: turn 1 runs at once with a PROMPT, else on the first stdin line; each later stdin line (an
unterminated last one too) is the next turn; turn k runs [steps, *turns][k], none past the end. A turn: the
UserPromptSubmit hooks (payload + prompt), the user line, the steps, the Stop hooks (payload + stop_hook_active false,
last_assistant_message the turn's last text or "", background_tasks `background` objects). Stdin's end: exit `exit`;
orphaned, or HANG_CAP s without a line: exit 1. Hooks: the --settings hooks.<event> entries whose matcher is absent,
"" or "*", or a regex re.search-ing the payload's tool_name (Notification's: notification_type; none: ""); each command
hook runs as /bin/sh -c in the fake's cwd and env, the payload JSON on stdin, stdout dropped; a failure is skipped.
Payload: session_id, transcript_path, cwd, hook_event_name.

Scenario: the JSON object in file $FAKE_CLAUDE_SCENARIO (unset, unreadable, a bad value or any other key: exit 1).
  steps       run in order; default []. A step of the other mode only is a bad value.
              A string: assistant text (interactive: also a stdout line).
              {"kind": "tool", "name": N}: an assistant tool_use of tool N (interactive: then PostToolUse, tool_name N).
              {"kind": "limit", "resets_at": T}, print: a rejected rate_limit_event (resetsAt T; null: none), then an
              assistant message with error "rate_limit" and text LIMIT.
              {"kind": "limit", "text": S}, interactive: prints S and ends the turn, without Stop.
              {"kind": "hook", "event": E, ...}, interactive: runs E's hooks, the step's other keys over the payload's.
              {"kind": "dialog", "text": S}, interactive: prints S, then takes a stdin line (its answer, no turn).
              {"kind": "sleep", "seconds": X}, interactive: sleeps X s (0 to HANG_CAP).
  turns       interactive: the steps of turn 2 on, a list of step lists; default [].
  background  interactive: how many background_tasks each Stop payload lists; default 0.
  rate_limit  print: a rate_limit_info object, printed as a rate_limit_event after init; default none.
  exit        the exit code; default 0.
  hang        print: true: after the steps, sleep until killed; it exits 1 by itself once orphaned or after HANG_CAP s.
  log         a file; on start the fake appends {"pid", "argv" (without argv[0]), "cwd"} to it as a JSON line.
"""
import argparse
import itertools
import json
import os
import re
import select
import shlex
import subprocess
import sys
import time
import uuid

ENV = "FAKE_CLAUDE_SCENARIO"
HANG_CAP = 120   # seconds
HOOK_TIMEOUT = 60   # seconds
DEFAULTS = {"steps": [], "turns": [], "background": 0, "rate_limit": None, "exit": 0, "hang": False, "log": None}
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


def _text(value) -> bool:
    return isinstance(value, str)


PRINT = {"tool": {"name": _text}, "limit": {"resets_at": lambda v: v is None or _number(v)}}
TUI = {"tool": {"name": _text}, "limit": {"text": _text}, "dialog": {"text": _text},
       "sleep": {"seconds": lambda v: _number(v) and 0 <= v <= HANG_CAP}}


def _step(step, tui: bool) -> bool:
    if isinstance(step, str):
        return True
    kind = step.get("kind") if isinstance(step, dict) else None
    if tui and kind == "hook":
        return isinstance(step.get("event"), str) and step["event"] != ""
    shape = (TUI if tui else PRINT).get(kind) if isinstance(kind, str) else None
    return shape is not None and set(step) == {"kind", *shape} and all(ok(step[key]) for key, ok in shape.items())


CHECKS = {"steps": lambda v: isinstance(v, list),
          "turns": lambda v: isinstance(v, list) and all(isinstance(t, list) for t in v),
          "background": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
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
    if a.output_format != ("stream-json" if a.print else None):
        ap.error("this fake models -p with --output-format stream-json, and interactive mode without either")
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
    if a.print and not a.verbose:
        sys.exit("Error: When using --print, --output-format=stream-json requires --verbose")
    return a


def _matches(matcher, field: str) -> bool:
    if matcher in ("", "*"):
        return True
    try:
        return isinstance(matcher, str) and re.search(matcher, field) is not None
    except re.error:
        return False


def run_hooks(hooks, event: str, payload: dict, field: str) -> None:
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        items = entry.get("hooks") if isinstance(entry, dict) and _matches(entry.get("matcher", ""), field) else None
        for hook in items if isinstance(items, list) else []:
            if isinstance(hook, dict) and hook.get("type") == "command" and isinstance(hook.get("command"), str):
                try:
                    subprocess.run(["/bin/sh", "-c", hook["command"]], input=json.dumps(payload), text=True,
                                   stdout=subprocess.DEVNULL, timeout=HOOK_TIMEOUT)
                except (OSError, subprocess.SubprocessError) as e:
                    print(f"fake_claude.py: {event} hook: {e}", file=sys.stderr, flush=True)


class Stranded(Exception):
    """Orphaned, or HANG_CAP s without a stdin line."""


class Stdin:
    """Stdin's lines, by os.read: select can't see what a buffered reader already holds."""

    def __init__(self, parent: int):
        self.parent, self.buf, self.eof = parent, b"", False

    def line(self) -> str:
        """The next line; EOFError at stdin's end, Stranded when orphaned or after HANG_CAP s."""
        until = time.monotonic() + HANG_CAP
        while b"\n" not in self.buf and not self.eof:
            if os.getppid() != self.parent or time.monotonic() > until:
                raise Stranded
            try:
                chunk = os.read(0, 65536) if select.select([0], [], [], 0.1)[0] else None
            except (OSError, ValueError):
                chunk = b""
            self.eof = chunk == b""
            self.buf += chunk or b""
        if not self.buf:
            raise EOFError
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode(errors="replace")


def interactive(s: dict, turns: list, hooks, base: dict, keep, stdin: Stdin, prompt: str | None) -> int:
    """Interactive mode's turns (the module's docstring); returns the exit code."""

    def hook(event: str, fields: dict) -> None:
        payload = {**base, "hook_event_name": event, **fields}
        field = payload.get("notification_type" if event == "Notification" else "tool_name")
        run_hooks(hooks, event, payload, field if isinstance(field, str) else "")

    def turn(text: str, steps: list) -> None:
        hook("UserPromptSubmit", {"prompt": text})
        keep("user", text)
        last = ""
        for k, step in enumerate(steps):
            kind = "text" if isinstance(step, str) else step["kind"]
            if kind == "text":
                print(step, flush=True)
                keep("assistant", [{"type": "text", "text": step}])
                last = step
            elif kind == "tool":
                keep("assistant", [{"type": "tool_use", "id": f"toolu_{k}", "name": step["name"], "input": {}}])
                hook("PostToolUse", {"tool_name": step["name"]})
            elif kind == "hook":
                hook(step["event"], {key: value for key, value in step.items() if key not in ("kind", "event")})
            elif kind == "sleep":
                time.sleep(step["seconds"])
            else:
                print(step["text"], flush=True)
                if kind == "limit":
                    return
                stdin.line()
        hook("Stop", {"stop_hook_active": False, "last_assistant_message": last,
                      "background_tasks": [{}] * s["background"]})

    try:
        prompt = prompt or stdin.line()
        for k in itertools.count():
            turn(prompt, turns[k] if k < len(turns) else [])
            prompt = stdin.line()
    except EOFError:
        return s["exit"]
    except Stranded:
        return 1


def main(argv: list[str]) -> int:
    parent = os.getppid()
    s = scenario()
    if s["log"]:
        with open(s["log"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "argv": argv, "cwd": os.getcwd()}) + "\n")
    a = parse(argv)
    turns = [s["steps"]] if a.print else [s["steps"], *s["turns"]]
    if not all(_step(step, not a.print) for steps in turns for step in steps):
        sys.exit("fake_claude.py: bad scenario steps")
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

    if not a.print:
        return interactive(s, turns, a.hooks, {"session_id": sid, "transcript_path": transcript, "cwd": cwd}, keep,
                           Stdin(parent), a.prompt)
    say({"type": "system", "subtype": "init", "cwd": cwd})
    if s["rate_limit"] is not None:
        say({"type": "rate_limit_event", "rate_limit_info": s["rate_limit"]})
    keep("user", a.prompt)
    for k, step in enumerate(s["steps"]):
        if isinstance(step, str):
            last = step
            assistant([{"type": "text", "text": step}])
        elif step["kind"] == "tool":
            assistant([{"type": "tool_use", "id": f"toolu_{k}", "name": step["name"], "input": {}}])
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
