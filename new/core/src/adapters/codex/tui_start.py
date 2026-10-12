from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sessions

HOOKS = (
    ("SessionStart", "sessionStart"),
    ("UserPromptSubmit", "userPromptSubmit"),
    ("PostToolUse", "postToolUse"),
    ("PermissionRequest", "permissionRequest"),
    ("Stop", "stop"),
    ("Interrupt", "interrupt"),
)
HASH = re.compile(r"sha256:[0-9a-f]{64}")
LOCK_WAIT = 30
ANSWER_WAIT = 20
EXIT_WAIT = 5
RETRY = 1
POLL = 0.1
BLOCK = 1 << 16
ANSWER_MAX = 64 << 20


class StartError(Exception):
    pass


class _Exited(Exception):
    pass


def hook_trust(
    exe: str, args: list[str], cwd: str, env: dict[str, str], commands: dict[str, str]
) -> str:
    root = sessions.store_directory(create=False)
    if root is None:
        raise StartError("codex hooks/list: missing session store")
    lock = os.path.join(
        root, hashlib.sha256(env["CODEX_HOME"].encode()).hexdigest() + ".start.lock"
    )
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise StartError(f"codex hooks/list: {lock}: {e.strerror}") from e
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise StartError("codex hooks/list: unsafe startup lock")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise StartError(
                        f"codex hooks/list: another Codex worker's start held {lock} for "
                        f"{LOCK_WAIT} s"
                    ) from None
                time.sleep(POLL)
        try:
            result = hooks_list(exe, args, cwd, env)
        except _Exited:
            time.sleep(RETRY)
            result = hooks_list(exe, args, cwd, env)
    except _Exited as e:
        raise StartError(f"codex hooks/list: app-server exited {e} before answering") from None
    finally:
        os.close(fd)
    return trusted(result, cwd, commands)


def hooks_list(exe: str, args: list[str], cwd: str, env: dict[str, str]):
    try:
        proc = subprocess.Popen(
            [exe, "app-server", *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        raise StartError(f"codex hooks/list: {exe}: {e.strerror}") from e
    lines = Lines(proc.stdout.fileno(), time.monotonic() + ANSWER_WAIT)
    exited = False
    try:
        send(
            proc,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-pm", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        answer(lines, 1, "initialize")
        send(proc, {"method": "initialized"})
        send(proc, {"id": 2, "method": "hooks/list", "params": {"cwds": [cwd]}})
        return answer(lines, 2, "hooks/list")
    except _Exited:
        exited = True
        raise
    finally:
        close(proc)
        if exited:
            raise _Exited(proc.returncode)


def send(proc, message: dict) -> None:
    try:
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()
    except BrokenPipeError:
        raise _Exited from None


class Lines:
    def __init__(self, fd: int, deadline: float):
        self.fd, self.deadline, self.buf, self.seen = fd, deadline, bytearray(), 0

    def __next__(self) -> bytes:
        while (end := self.buf.find(b"\n", self.seen)) < 0:
            self.seen = len(self.buf)
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise StartError(f"codex hooks/list: no answer in {ANSWER_WAIT} s")
            if not select.select([self.fd], [], [], left)[0]:
                continue
            chunk = os.read(self.fd, BLOCK)
            if not chunk:
                raise _Exited
            if len(self.buf) + len(chunk) > ANSWER_MAX:
                raise StartError(f"codex hooks/list: an answer over {ANSWER_MAX} bytes")
            self.buf += chunk
        line = bytes(self.buf[:end])
        del self.buf[: end + 1]
        self.seen = 0
        return line


def answer(lines: Lines, ident: int, method: str):
    while True:
        try:
            message = json.loads(next(lines))
        except (ValueError, RecursionError):
            continue
        if (
            not isinstance(message, dict)
            or "method" in message
            or type(message.get("id")) is not int
            or message["id"] != ident
        ):
            continue
        if "result" in message:
            return message["result"]
        error = message.get("error")
        raise StartError(
            f"codex hooks/list: {method} failed: "
            f"{error.get('message') if isinstance(error, dict) else error}"
        )


def close(proc) -> None:
    deadline = time.monotonic() + EXIT_WAIT
    try:
        proc.stdin.close()
    except OSError:
        pass
    fd = proc.stdout.fileno()
    try:
        while (left := deadline - time.monotonic()) > 0 and select.select([fd], [], [], left)[0]:
            if not os.read(fd, BLOCK):
                break
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    finally:
        proc.stdout.close()


def trusted(result, cwd: str, commands: dict[str, str]) -> str:
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, list) else []
    if len(data) != 1:
        raise StartError(f"codex hooks/list: want one answer for {cwd}, got {len(data)}")
    entry = data[0] if isinstance(data[0], dict) else {}
    if entry.get("errors"):
        raise StartError(
            f"codex hooks/list: errors for {cwd}: {json.dumps(entry['errors'], ensure_ascii=False)}"
        )
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or not all(isinstance(h, dict) for h in hooks):
        raise StartError(f"codex hooks/list: malformed hooks for {cwd}")
    if not hooks:
        raise StartError(
            "codex hooks/list: Codex hooks are off (features.hooks); a Codex worker needs them"
        )
    ours = [h for h in hooks if h.get("source") == "sessionFlags"]
    trusted = []
    for event, name in HOOKS:
        command = commands[event]
        rows = [h for h in ours if h.get("eventName") == name]
        if len(rows) != 1:
            raise StartError(
                f"codex hooks/list: {len(rows) or 'no'} session-flags {name} hook"
                f"{'s' if len(rows) > 1 else ''}"
            )
        h = rows[0]
        why = (
            "is disabled"
            if h.get("enabled") is not True
            else "is not our command"
            if h.get("handlerType") != "command" or h.get("command") != command
            else "has no key"
            if not isinstance(h.get("key"), str)
            else "has no sha256 hash"
            if not isinstance(h.get("currentHash"), str) or not HASH.fullmatch(h["currentHash"])
            else ""
        )
        if why:
            raise StartError(f"codex hooks/list: the session-flags {name} hook {why}")
        trusted.append(h)
    for h in ours:
        if not any(h is t for t in trusted):
            raise StartError(f"codex hooks/list: a session-flags hook not ours: {h.get('key')}")
    for h in hooks:
        if (
            h.get("source") != "sessionFlags"
            and h.get("enabled") is not False
            and h.get("isManaged") is not True
            and h.get("trustStatus") in ("untrusted", "modified")
        ):
            raise StartError(
                f"codex hooks/list: your hook {h.get('key')} is {h['trustStatus']}: review it in your "
                "own Codex session (/hooks) first"
            )
    return (
        "hooks.state={"
        + ",".join(
            f"{json.dumps(h['key'], ensure_ascii=False)}="
            f"{{trusted_hash={json.dumps(h['currentHash'])}}}"
            for h in trusted
        )
        + "}"
    )


def main(argv):
    if len(argv) != 1 or len(argv[0].encode("utf-8")) > ANSWER_MAX:
        raise StartError("Codex tui launch: expected bounded launch data")
    try:
        data = json.loads(argv[0])
    except (ValueError, RecursionError) as exc:
        raise StartError("Codex tui launch: malformed launch data") from exc
    if not isinstance(data, dict) or set(data) != {
        "argv",
        "hook",
        "commands",
        "hook_flags",
        "workdir",
        "sid",
    }:
        raise StartError("Codex tui launch: invalid launch fields")
    for key in ("argv", "hook", "hook_flags"):
        if (
            not isinstance(data[key], list)
            or not data[key]
            or any(not isinstance(w, str) or "\0" in w for w in data[key])
        ):
            raise StartError("Codex tui launch: invalid command")
    if not os.path.isabs(data["argv"][0]) or not os.path.isabs(data["hook"][0]):
        raise StartError("Codex tui launch: expected absolute executables")
    commands = data["commands"]
    if (
        not isinstance(commands, dict)
        or set(commands) != {event for event, name in HOOKS}
        or any(not isinstance(word, str) or not word for word in commands.values())
    ):
        raise StartError("Codex tui launch: invalid hook commands")
    state = sessions.load_session(data["workdir"], data["sid"])
    if state is None:
        raise StartError("Codex tui launch: missing recovery state")
    directory = os.path.dirname(os.path.abspath(__file__))
    sessions.protect(
        state,
        [
            os.path.dirname(os.path.dirname(directory)),
            sys.prefix,
            sys.base_prefix,
            sys.executable,
            data["argv"][0],
            sessions.store_root(),
            *(word for word in data["hook"] if os.path.isabs(word)),
        ],
    )
    cwd = state.snapshot["cwd"]
    env = {**os.environ, "CODEX_HOME": state.snapshot["codex_home"]}
    trusted_state = hook_trust(data["argv"][0], data["hook_flags"], cwd, env, commands)
    command = data["argv"].copy()
    index = command.index("--") if "--" in command else len(command)
    command[index:index] = ["-c", trusted_state]
    os.chdir(cwd)
    os.execve(command[0], command, env)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (StartError, sessions.RecoveryError, OSError, ValueError) as exc:
        print(f"codex tui: {exc}", file=sys.stderr)
        sys.exit(1)
