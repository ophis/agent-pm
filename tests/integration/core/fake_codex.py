import argparse
import hashlib
import json
import os
import re
import select
import shlex
import subprocess
import sys
import tomllib
import uuid

if __package__:
    from tests.integration.core import fake_claude
else:   # install()'s shim runs this file as a script
    import fake_claude


ENV = "FAKE_CODEX_SCENARIO"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"
SESSION_FLAGS = "/<session-flags>/config.toml"
LOGGED = ("CODEX_HOME", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_VERSION", "CODEX_SANDBOX",
          "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_PERMISSION_PROFILE", "CODEX_CI", "CLAUDECODE", "NO_COLOR", "PAGER",
          "GIT_PAGER", "GH_PAGER", "TERM", "COLORTERM")
DEFAULTS = {"steps": [], "turns": [], "exit": 0, "log": None, "user_hook": None, "errors": []}
USER_HOOK = {"event": "Stop", "command": "true", "trustStatus": "trusted", "enabled": True, "isManaged": False}
STEPS = {"permission": {"kind", "tool_name", "tool_input", "agent_id", "agent_type"}, "interrupt": {"kind"}}
SHORT = ("Interrupt", "SessionEnd")


def install(bin_dir: str) -> str:
    for name, target in (("codex", f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"),
                         ("python3", shlex.quote(sys.executable))):
        path = os.path.join(bin_dir, name)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nexec {target} "$@"\n')
        os.chmod(path, 0o755)
    return f"{bin_dir}:/usr/bin:/bin"


def parse(argv: list[str]) -> argparse.Namespace:
    if not argv or argv[0] != "exec":
        sys.exit("fake_codex.py: expected exec")
    ap = argparse.ArgumentParser(prog="codex exec", allow_abbrev=False)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-git-repo-check", action="store_true")
    ap.add_argument("-s", "--sandbox", choices=("workspace-write",))
    ap.add_argument("-m", "--model")
    ap.add_argument("-c", "--config", action="append", default=[])
    ap.add_argument("--add-dir", action="append", default=[])
    resume = "resume" in argv
    common = argv[1:argv.index("resume")] if resume else argv[1:-1]
    ap.add_argument("prompt", nargs="?")
    args = ap.parse_args(common)
    if resume:
        suffix = argv[argv.index("resume") + 1:]
        if len(suffix) != 2:
            sys.exit("fake_codex.py: expected resume ID and prompt")
        args.resume_id, args.prompt = suffix
    else:
        args.resume_id, args.prompt = None, argv[-1]
    if not (args.json and args.skip_git_repo_check
            and args.sandbox == "workspace-write" and args.model
            and 'approval_policy="on-request"' in args.config
            and 'approvals_reviewer="auto_review"' in args.config):
        sys.exit("fake_codex.py: missing required headless launch settings")
    if not any(value.startswith("model_reasoning_effort=") and value.split("=", 1)[1] for value in args.config):
        sys.exit("fake_codex.py: missing model reasoning effort")
    if not args.prompt:
        sys.exit("fake_codex.py: missing prompt")
    return args


def scenario() -> dict:
    try:
        with open(os.environ[ENV]) as f:
            data = json.load(f)
    except (KeyError, OSError, ValueError) as e:
        sys.exit(f"fake_codex.py: scenario: {e}")
    if (not isinstance(data, dict) or not {"steps", "log"} <= set(data)
            or set(data) - {"steps", "log", "thread_id"} or not isinstance(data["steps"], list)):
        sys.exit("fake_codex.py: expected steps and log")
    for step in data["steps"]:
        if isinstance(step, str):
            continue
        if not isinstance(step, dict) or step.get("kind") not in ("progress", "outcome", "stderr"):
            sys.exit("fake_codex.py: invalid step")
    return data


def rollout(home: str, client_id: str) -> str:
    return os.path.join(home, "sessions", "2026", "10", "10", f"rollout-2026-10-10-{client_id}.jsonl")


def session_path(client_id: str) -> str:
    home = os.environ.get("CODEX_HOME")
    if not home:
        sys.exit("fake_codex.py: missing CODEX_HOME")
    return rollout(home, client_id)


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def exec_mode(argv: list[str]) -> int:
    args = parse(argv)
    data = scenario()
    # The driver must close stdin. A one-second timeout makes an accidental open pipe fail promptly.
    ready, _, _ = select.select([sys.stdin], [], [], 1)
    stdin_eof = bool(ready) and os.read(sys.stdin.fileno(), 1) == b""
    if not stdin_eof:
        sys.exit("fake_codex.py: stdin was not closed")
    if args.resume_id:
        try:
            uuid.UUID(args.resume_id)
        except ValueError:
            sys.exit("fake_codex.py: invalid resume ID")
        if not os.path.isfile(session_path(args.resume_id)):
            sys.exit("fake_codex.py: resumed session file absent")
    else:
        path = session_path(CLIENT_ID)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": CLIENT_ID, "cwd": os.getcwd()}}) + "\n")
    with open(data["log"], "w") as f:
        json.dump({"argv": argv, "cwd": os.getcwd(), "prompt": args.prompt,
                   "stdin_eof": stdin_eof, "resume_id": args.resume_id}, f)
    command = fake_claude.report_command([args.prompt])
    if command is None:
        sys.exit("fake_codex.py: no report command in prompt")
    emit({"type": "thread.started", "thread_id": data.get("thread_id", CLIENT_ID)})
    emit({"type": "turn.started"})
    for step in data["steps"]:
        if isinstance(step, str):
            emit({"type": "item.completed", "item": {"type": "agent_message", "text": step}})
        elif step["kind"] == "stderr":
            print(step["text"], file=sys.stderr, flush=True)
        else:
            fake_claude.report(command, step)
    emit({"type": "turn.completed"})
    return 0


def session_scenario() -> dict:
    path = os.environ.get(ENV)
    if not path:
        sys.exit(f"fake_codex.py: {ENV} is unset")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"fake_codex.py: {ENV}: {e}")
    if not isinstance(data, dict):
        sys.exit("fake_codex.py: the scenario is no JSON object")
    if extra := sorted(set(data) - set(DEFAULTS)):
        sys.exit(f"fake_codex.py: unknown scenario key {extra[0]!r}")
    data = {**DEFAULTS, **data}
    if not (isinstance(data["turns"], list) and isinstance(data["errors"], list) and type(data["exit"]) is int):
        sys.exit("fake_codex.py: turns and errors: want lists; exit: want an int")
    for steps in [data["steps"], *data["turns"]]:
        if not isinstance(steps, list):
            sys.exit(f"fake_codex.py: bad steps {steps!r}")
        for step in steps:
            kind = step.get("kind") if isinstance(step, dict) else None
            allowed = STEPS.get(kind) if isinstance(kind, str) else None
            if not isinstance(step, str) and (allowed is None or not set(step) <= allowed or not all(
                    isinstance(step.get(k, ""), str) for k in ("tool_name", "agent_id", "agent_type"))):
                sys.exit(f"fake_codex.py: bad step {step!r}")
    if (hook := data["user_hook"]) is not None:
        if not (isinstance(hook, dict) and set(hook) <= set(USER_HOOK)
                and all(type(hook[k]) is type(USER_HOOK[k]) for k in hook)):
            sys.exit(f"fake_codex.py: bad user_hook {hook!r}")
        data["user_hook"] = {**USER_HOOK, **hook}
    return data


def log(s: dict, line: dict) -> None:
    if s["log"]:
        with open(s["log"], "a") as f:
            f.write(json.dumps({"pid": os.getpid(), **line}) + "\n")


def start(s: dict, argv: list[str]) -> None:
    log(s, {"argv": argv, "cwd": os.getcwd(), "env": {k: os.environ[k] for k in LOGGED if k in os.environ}})


class Override(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if self.dest == "config" and "=" not in value:
            parser.error(f"{option_string} {value}: want KEY=VALUE")
        namespace.overrides = [*namespace.overrides, (self.dest, value)]


def parser(prog: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, allow_abbrev=False)
    ap.set_defaults(overrides=[])
    ap.add_argument("-c", "--config", action=Override)
    for flag in ("--enable", "--disable"):
        ap.add_argument(flag, action=Override)
    return ap


def configure(overrides: list[tuple[str, str]]) -> dict:
    config = {}
    for dest, value in overrides:
        if dest == "config":
            key, _, raw = value.partition("=")
            try:
                value = tomllib.loads(f"v = {raw}")["v"]
            except tomllib.TOMLDecodeError:
                value = raw
            path = key.strip().split(".")
        else:
            path, value = ["features", value], dest == "enable"
        node = config
        for part in path[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[path[-1]] = value
    return config


def hooks_on(config: dict) -> bool:
    features = config.get("features")
    return not (isinstance(features, dict) and features.get("hooks") is False)


def snake(event: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def entry(event: str, key: str, source_path: str, source: str, command: str, timeout, sync, matcher) -> dict:
    if type(timeout) is not int:
        timeout = 1 if event in SHORT else 600
    timeout = min(max(timeout, 1), 3) if event in SHORT else max(timeout, 1)
    body = {"event_name": snake(event),
            "hooks": [{"async": sync is True, "command": command, "timeout": timeout, "type": "command"}],
            **({} if matcher is None else {"matcher": matcher})}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    return {"key": key, "eventName": event[:1].lower() + event[1:], "handlerType": "command", "command": command,
            "matcher": matcher, "timeoutSec": timeout, "sourcePath": source_path, "source": source, "enabled": True,
            "isManaged": False, "currentHash": f"sha256:{digest.hexdigest()}", "trustStatus": "untrusted"}


def catalog(config: dict, home: str, user_hook: dict | None) -> list[dict]:
    if not hooks_on(config):
        return []
    hooks = config["hooks"] if isinstance(config.get("hooks"), dict) else {}
    states = hooks["state"] if isinstance(hooks.get("state"), dict) else {}
    entries = []
    for event, groups in hooks.items():
        for g, group in enumerate(groups if event != "state" and isinstance(groups, list) else []):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for h, handler in enumerate(handlers if isinstance(handlers, list) else []):
                if not (isinstance(handler, dict) and handler.get("type") == "command"
                        and isinstance(handler.get("command"), str)):
                    continue
                key = f"{SESSION_FLAGS}:{snake(event)}:{g}:{h}"
                e = entry(event, key, SESSION_FLAGS, "sessionFlags", handler["command"], handler.get("timeout"),
                          handler.get("async"), group.get("matcher"))
                state = states[key] if isinstance(states.get(key), dict) else {}
                trusted = state.get("trusted_hash")
                e["enabled"] = state.get("enabled") is not False
                e["trustStatus"] = ("trusted" if trusted == e["currentHash"] else
                                    "untrusted" if trusted is None else "modified")
                entries.append(e)
    if user_hook:
        path, event = os.path.join(home, "hooks.json"), user_hook["event"]
        e = entry(event, f"{path}:{snake(event)}:0:0", path, "user", user_hook["command"], None, False, None)
        e.update({k: user_hook[k] for k in ("enabled", "isManaged", "trustStatus")})
        entries.append(e)
    return entries


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def app_server(argv: list[str]) -> int:
    s = session_scenario()
    start(s, argv)
    config = configure(parser("codex app-server").parse_args(argv[1:]).overrides)
    hooks = catalog(config, codex_home(), s["user_hook"])
    errors = s["errors"] if hooks_on(config) else []
    ready = False

    def say(message: dict) -> None:
        print(json.dumps(message), flush=True)

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            print(f"fake_codex.py: app-server: not JSON: {line!r}", file=sys.stderr, flush=True)
            continue
        log(s, {"received": msg})
        if not isinstance(msg, dict) or "id" not in msg:
            continue
        say({"method": "fake/notice", "params": {"before": msg["id"]}})
        method, params, ident = msg.get("method"), msg.get("params"), msg["id"]
        cwds = params.get("cwds") if isinstance(params, dict) else None
        if method == "initialize":
            ready = True
            say({"id": ident, "result": {"userAgent": "fake_codex/0.162.1"}})
        elif not ready:
            say({"id": ident, "error": {"code": -32002, "message": "Not initialized"}})
        elif method != "hooks/list":
            say({"id": ident, "error": {"code": -32601, "message": f"unknown method {method!r}"}})
        elif not (isinstance(cwds, list) and all(isinstance(c, str) for c in cwds)):
            say({"id": ident, "error": {"code": -32602, "message": "hooks/list: want cwds, a list of strings"}})
        else:
            say({"id": ident, "result": {"data": [{"cwd": c, "hooks": hooks, "warnings": [], "errors": errors}
                                                  for c in cwds]}})
    return 0


def interactive(argv: list[str]) -> int:
    parent = os.getppid()
    s = session_scenario()
    start(s, argv)
    ap = parser("codex")
    resumed = None
    if argv[:1] == ["resume"]:
        if len(argv) < 2 or argv[1].startswith("-"):
            ap.error("resume: want an ID")
        resumed, argv = argv[1], argv[2:]
    ap.add_argument("-C", "--cd")
    ap.add_argument("--no-daemon", action="store_true")
    for short, long in (("-m", "--model"), ("-s", "--sandbox"), ("-a", "--ask-for-approval")):
        ap.add_argument(short, long)
    ap.add_argument("--add-dir", action="append", default=[])
    ap.add_argument("prompt", nargs="?")
    a = ap.parse_args(argv)
    cwd = os.path.abspath(a.cd) if a.cd else os.getcwd()
    try:
        os.chdir(cwd)
    except OSError as e:
        sys.exit(f"fake_codex.py: -C {a.cd}: {e}")
    home = codex_home()
    sid = resumed or str(uuid.uuid4())
    transcript = rollout(home, sid)
    if resumed and not os.path.isfile(transcript):
        sys.exit(f"fake_codex.py: no session {resumed}")
    hooks = catalog(configure(a.overrides), home, s["user_hook"])
    if review := [h for h in hooks if h["enabled"] and not h["isManaged"]
                  and h["trustStatus"] in ("untrusted", "modified")]:
        n = len(review)
        print("Hooks need review", f"{n} hook is new or changed." if n == 1 else f"{n} hooks are new or changed.",
              "1. Review hooks", "2. Trust all and continue", "3. Continue without trusting (hooks won't run)",
              sep="\n", flush=True)
        try:
            for _ in fake_claude.inputs(None, parent):
                pass
        except fake_claude.Stranded:
            return 1
        return s["exit"]
    color = os.environ.get("NO_COLOR", "0") == "0"
    session_start = "resume" if resumed else "startup"

    def fire(event: str, **extra) -> None:
        payload = {"session_id": sid, "transcript_path": transcript, "cwd": cwd, "hook_event_name": event,
                   "model": a.model or "fake-model", "permission_mode": "default", **extra}
        for h in hooks:
            if (h["eventName"] != event[:1].lower() + event[1:] or not h["enabled"]
                    or h["trustStatus"] not in ("trusted", "managed") or h["matcher"] not in (None, "", "*")):
                continue
            try:
                res = subprocess.run(["/bin/sh", "-c", h["command"]], input=json.dumps(payload), cwd=cwd,
                                     stdout=subprocess.PIPE, text=True, errors="replace", timeout=h["timeoutSec"])
                code, out = res.returncode, res.stdout
            except (OSError, subprocess.SubprocessError) as e:
                print(f"fake_codex.py: {event} hook: {e}", file=sys.stderr, flush=True)
                code = out = None
            log(s, {"hook": event, "key": h["key"], "payload": payload, "exit": code, "stdout": out})

    def turn(prompt: str, steps: list) -> None:
        nonlocal session_start
        if not os.path.isfile(transcript):
            os.makedirs(os.path.dirname(transcript), exist_ok=True)
            with open(transcript, "w") as f:
                f.write(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}) + "\n")
        if session_start:
            fire("SessionStart", source=session_start)
            session_start = None
        turn_id, last = str(uuid.uuid4()), None
        fire("UserPromptSubmit", turn_id=turn_id, prompt=prompt)
        for step in steps:
            if isinstance(step, str):
                last = step
                print(f"\x1b[36m{step}\x1b[0m" if color else step, flush=True)
            elif step["kind"] == "interrupt":
                fire("Interrupt", turn_id=turn_id)
                return
            else:
                fire("PermissionRequest", turn_id=turn_id, tool_name=step.get("tool_name", "Bash"),
                     tool_input=step.get("tool_input", {"command": "true", "description": "fake"}),
                     **{k: step[k] for k in ("agent_id", "agent_type") if k in step})
        fire("Stop", turn_id=turn_id, stop_hook_active=False, last_assistant_message=last)

    turns = [s["steps"], *s["turns"]]
    try:
        for k, prompt in enumerate(fake_claude.inputs(a.prompt, parent)):
            turn(prompt, turns[k] if k < len(turns) else [])
    except fake_claude.Stranded:
        return 1
    return s["exit"]


def main(argv: list[str]) -> int:
    if argv[:1] == ["exec"]:
        return exec_mode(argv)
    return app_server(argv) if argv[:1] == ["app-server"] else interactive(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
