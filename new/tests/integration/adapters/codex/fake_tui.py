import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_codex

EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "Interrupt",
)


def install(directory):
    path = Path(directory) / "codex"
    path.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + " -I "
        + shlex.quote(__file__)
        + ' "$@"\n'
    )
    path.chmod(0o755)
    return str(path)


def scenario():
    return json.loads(Path(os.environ["FAKE_SCENARIO"]).read_text())


def log(value):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def started(mode, args):
    log(
        {
            "mode": mode,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "cwd": os.getcwd(),
            "argv": args,
            "env": {
                key: os.environ.get(key)
                for key in (
                    *fake_codex.MARKERS,
                    *fake_codex.DEFAULTS,
                    "CODEX_HOME",
                    "HOME",
                    "PWD",
                    "TMPDIR",
                    "FAKE_ADDITION",
                )
            },
        }
    )


def configure(overrides):
    config = {}
    for value in overrides:
        key, separator, raw = value.partition("=")
        if not separator:
            raise ValueError("expected key=value")
        try:
            parsed = tomllib.loads("value=" + raw)["value"]
        except tomllib.TOMLDecodeError:
            parsed = raw
        node, parts = config, key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = parsed
    return config


def catalog(config, user=None):
    hooks = config.get("hooks", {})
    state = hooks.get("state", {})
    rows = []
    for event in EVENTS:
        for index, group in enumerate(hooks.get(event, [])):
            for handler_index, handler in enumerate(group["hooks"]):
                snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
                command = handler["command"]
                timeout = handler.get("timeout", 3 if event == "Interrupt" else 600)
                body = {
                    "event_name": snake,
                    "hooks": [
                        {"async": False, "command": command, "timeout": timeout, "type": "command"}
                    ],
                }
                digest = hashlib.sha256(
                    json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                key = f"/<session-flags>/config.toml:{snake}:{index}:{handler_index}"
                trust = state.get(key, {})
                hashed = "sha256:" + digest
                rows.append(
                    {
                        "source": "sessionFlags",
                        "eventName": event[0].lower() + event[1:],
                        "handlerType": "command",
                        "command": command,
                        "key": key,
                        "currentHash": hashed,
                        "timeoutSec": timeout,
                        "enabled": trust.get("enabled", True),
                        "isManaged": False,
                        "trustStatus": "trusted"
                        if trust.get("trusted_hash") == hashed
                        else "modified"
                        if trust.get("trusted_hash") is not None
                        else "untrusted",
                    }
                )
    if user:
        rows.append(
            {
                "source": "user",
                "eventName": "stop",
                "handlerType": "command",
                "command": ":",
                "key": os.environ["CODEX_HOME"] + "/hooks.json:stop:0:0",
                "currentHash": "sha256:" + "f" * 64,
                "timeoutSec": 3,
                "enabled": user != "disabled",
                "isManaged": user == "managed",
                "trustStatus": "trusted" if user in ("disabled", "managed") else user,
            }
        )
    return rows


def emit(value):
    print(json.dumps(value), flush=True)


def parser():
    result = argparse.ArgumentParser(allow_abbrev=False)
    result.add_argument("-c", action="append", default=[])
    return result


def app_server(args):
    scene = scenario()
    config = configure(parser().parse_args(args).c)
    started("app-server", ["app-server", *args])
    case = scene.get("catalog_case")
    if case == "exit":
        return 1
    if case == "silent_tree":
        subprocess.Popen([sys.executable, "-I", __file__, "stalled-child", "2"])
    if case in ("silent", "silent_tree"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)
        return 0
    initialized = False
    for line in sys.stdin:
        message = json.loads(line)
        log({"received": message})
        if "id" not in message:
            continue
        ident = message["id"]
        emit({"method": "fake/notice", "params": {"before": ident}})
        print("not JSON", flush=True)
        emit({"id": str(ident), "result": "wrong id type"})
        emit({"id": ident + 100, "result": "unrelated response"})
        method = message["method"]
        if method == "initialize":
            initialized = True
            emit({"id": ident, "result": {"userAgent": "fake-tui"}})
        elif not initialized or method != "hooks/list":
            emit({"id": ident, "error": {"message": "not initialized or unknown method"}})
        else:
            time.sleep(scene.get("catalog_delay", 0))
            rows = catalog(config, scene.get("user_hook"))
            if case == "off":
                rows = []
            elif case == "disabled":
                rows[0]["enabled"] = False
            elif case == "foreign":
                rows.append({"source": "sessionFlags", "eventName": "foreign"})
            elif case == "missing":
                rows.pop()
            elif case == "duplicate":
                rows.append(rows[0])
            elif case == "command":
                rows[0]["command"] = "foreign command"
            elif case == "hash":
                rows[0]["currentHash"] = "invalid hash"
            log({"catalog": rows})
            emit(
                {
                    "id": ident,
                    "result": {
                        "data": [
                            {
                                "cwd": cwd,
                                "hooks": None if case == "malformed" else rows,
                                "errors": [{"message": "bad hook file"}]
                                if case == "errors"
                                else [],
                                "warnings": [],
                            }
                            for cwd in message["params"]["cwds"]
                        ]
                    },
                }
            )
    log({"probe_closed": os.getpid()})
    return 0


def interactive(args):
    scene = scenario()
    resumed = None
    if args[:1] == ["resume"]:
        if len(args) < 2 or args[1].startswith("-"):
            parser().error("resume requires a native ID")
        resumed, args = args[1], args[2:]
    options = parser()
    options.add_argument("--no-daemon", action="store_true", required=True)
    options.add_argument("-C", required=True)
    options.add_argument("-s", choices=("danger-full-access", "workspace-write"))
    options.add_argument("-m")
    options.add_argument("--add-dir", action="append", default=[])
    options.add_argument("prompt", nargs="?")
    parsed = options.parse_args(args)
    os.chdir(parsed.C)
    started("tui", [*(["resume", resumed] if resumed else []), *args])
    rows = catalog(configure(parsed.c), scene.get("user_hook"))
    if any(
        row["enabled"] and not row["isManaged"] and row["trustStatus"] != "trusted" for row in rows
    ):
        print("Hooks need review", flush=True)
        for _ in sys.stdin:
            pass
        return 1
    native = scene.get("thread_id", resumed or str(uuid.uuid4()))
    path = fake_codex.session_path(native)
    if resumed and not path.is_file():
        print("no session " + resumed, file=sys.stderr, flush=True)
        return 1
    initial = "resume" if resumed else "startup"
    color = os.environ.get("NO_COLOR", "0") == "0"

    def fire(label, extra=None):
        payload = {
            "session_id": native,
            "cwd": os.getcwd(),
            "transcript_path": str(path),
            "hook_event_name": label,
            "model": parsed.m or "fake-model",
            "permission_mode": "default",
            **(extra or {}),
        }
        for row in rows:
            if row["eventName"] != label[0].lower() + label[1:] or not row["enabled"]:
                continue
            result = subprocess.run(
                ["/bin/sh", "-c", row["command"]],
                input=json.dumps(payload),
                cwd=os.getcwd(),
                text=True,
                capture_output=True,
                timeout=row["timeoutSec"],
            )
            log(
                {
                    "hook": label,
                    "source": row["source"],
                    "payload": payload,
                    "key": row["key"],
                    "pid": os.getpid(),
                    "parent_pid": os.getppid(),
                    "cwd": os.getcwd(),
                    "exit": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )

    def turn(text, steps):
        nonlocal initial
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": native, "cwd": os.getcwd()}})
                + "\n"
            )
        with path.open("a") as stream:
            stream.write(json.dumps({"type": "user", "text": text}) + "\n")
        if initial:
            fire("SessionStart", {"source": initial})
            initial = None
        ident, last = str(uuid.uuid4()), None
        fire("UserPromptSubmit", {"prompt": text, "turn_id": ident})
        for step in steps:
            if isinstance(step, str):
                last = step
                print("\x1b[36m" + step + "\x1b[0m" if color else step, flush=True)
            elif step["kind"] == "permission":
                fire(
                    "PermissionRequest",
                    {
                        "turn_id": ident,
                        "tool_name": step.get("tool_name", "Bash"),
                        "tool_input": {"command": "true"},
                    },
                )
            elif step["kind"] == "tool":
                fire("PostToolUse", {"turn_id": ident, "tool_name": step.get("tool_name", "Bash")})
            elif step["kind"] == "hook":
                fire(step["label"], step.get("payload"))
            elif step["kind"] == "interrupt":
                fire("Interrupt", {"turn_id": ident})
                return
            elif step["kind"] == "screen":
                print("\x1b[2J\x1b[H" + step["text"], flush=True)
            elif step["kind"] == "pause":
                while not Path(step["file"]).exists():
                    time.sleep(0.02)
            else:
                raise ValueError("unknown interactive step")
        fire("Stop", {"turn_id": ident, "stop_hook_active": False, "last_assistant_message": last})

    turns, index = scene.get("tui_turns", [[]]), 0
    if parsed.prompt is not None:
        turn(parsed.prompt, turns[index] if index < len(turns) else [])
        index += 1
    for line in sys.stdin:
        turn(line.rstrip("\n"), turns[index] if index < len(turns) else [])
        index += 1
    return 0


def main(args):
    if args[:1] == ["stalled-child"]:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        log({"descendant": os.getpid()})
        if int(args[1]) > 1:
            subprocess.Popen(
                [sys.executable, "-I", __file__, "stalled-child", str(int(args[1]) - 1)]
            )
        time.sleep(30)
        return 0
    if args[:1] == ["exec"]:
        return fake_codex.main()
    return app_server(args[1:]) if args[:1] == ["app-server"] else interactive(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
