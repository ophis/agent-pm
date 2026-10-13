from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import runpy
import signal
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_pm.session.adapters.codex.server.types import ProcessRequest

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


_sibling = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["sibling"]

sessions = _sibling("sessions")
app_server = _sibling("app_server")
HOOKS = _sibling("hook_schema").HOOKS
if not TYPE_CHECKING:
    ProcessRequest = app_server.ProcessRequest

HASH = re.compile(r"sha256:[0-9a-f]{64}")
LOCK_WAIT = 30
ANSWER_WAIT = 20
RETRY = 1
POLL = 0.1
ANSWER_MAX = 64 << 20


class StartError(Exception):
    @classmethod
    def missing_store(cls) -> StartError:
        return cls("codex hooks/list: missing session store")

    @classmethod
    def lock_access(cls, path: str, cause: OSError) -> StartError:
        return cls(f"codex hooks/list: {path}: {cause.strerror}")

    @classmethod
    def unsafe_lock(cls) -> StartError:
        return cls("codex hooks/list: unsafe startup lock")

    @classmethod
    def held_lock(cls, path: str, seconds: float) -> StartError:
        return cls(f"codex hooks/list: another Codex worker's start held {path} for {seconds} s")

    @classmethod
    def early_exit(cls, cause: Exception) -> StartError:
        return cls(f"codex hooks/list: app-server exited {cause} before answering")

    @classmethod
    def listing_failed(cls, cause: Exception) -> StartError:
        return cls(f"codex hooks/list: {cause}")

    @classmethod
    def answer_count(cls, cwd: str, count: int) -> StartError:
        return cls(f"codex hooks/list: want one answer for {cwd}, got {count}")

    @classmethod
    def listing_errors(cls, cwd: str, errors: object) -> StartError:
        return cls(f"codex hooks/list: errors for {cwd}: {json.dumps(errors, ensure_ascii=False)}")

    @classmethod
    def malformed_hooks(cls, cwd: str) -> StartError:
        return cls(f"codex hooks/list: malformed hooks for {cwd}")

    @classmethod
    def disabled_hooks(cls) -> StartError:
        return cls(
            "codex hooks/list: Codex hooks are off (features.hooks); a Codex worker needs them"
        )

    @classmethod
    def hook_count(cls, name: str, count: int) -> StartError:
        return cls(
            f"codex hooks/list: {count or 'no'} session-flags {name} hook{'s' if count > 1 else ''}"
        )

    @classmethod
    def invalid_hook(cls, name: str, hook: dict, command: str) -> StartError:
        return cls(
            f"codex hooks/list: the session-flags {name} hook {cls._hook_problem(hook, command)}"
        )

    @classmethod
    def foreign_hook(cls, key: object) -> StartError:
        return cls(f"codex hooks/list: a session-flags hook not ours: {key}")

    @classmethod
    def unreviewed_hook(cls, key: object, status: str) -> StartError:
        return cls(
            f"codex hooks/list: your hook "
            f"{key}"
            f" is "
            f"{status}"
            f": review it in your own Codex session (/hooks) "
            f"first"
        )

    @classmethod
    def oversized_launch(cls) -> StartError:
        return cls("Codex tui launch: expected bounded launch data")

    @classmethod
    def malformed_launch(cls) -> StartError:
        return cls("Codex tui launch: malformed launch data")

    @classmethod
    def launch_fields(cls) -> StartError:
        return cls("Codex tui launch: invalid launch fields")

    @classmethod
    def launch_command(cls) -> StartError:
        return cls("Codex tui launch: invalid command")

    @classmethod
    def relative_executables(cls) -> StartError:
        return cls("Codex tui launch: expected absolute executables")

    @classmethod
    def hook_commands(cls) -> StartError:
        return cls("Codex tui launch: invalid hook commands")

    @classmethod
    def launch_paths(cls, field: str) -> StartError:
        return cls(f"Codex tui launch: invalid {field} paths")

    @classmethod
    def missing_state(cls) -> StartError:
        return cls("Codex tui launch: missing recovery state")

    @staticmethod
    def _hook_problem(hook: dict, command: str) -> str:
        if hook.get("enabled") is not True:
            return "is disabled"
        if hook.get("handlerType") != "command" or hook.get("command") != command:
            return "is not our command"
        if not isinstance(hook.get("key"), str):
            return "has no key"
        if not isinstance(hook.get("currentHash"), str) or not HASH.fullmatch(hook["currentHash"]):
            return "has no sha256 hash"
        return ""


class Stopped(BaseException):
    def __init__(self, signum):
        self.signum = signum


class _Termination:
    def __init__(self):
        self.signum = 0
        self.previous = {}

    def __enter__(self):
        for signum in (signal.SIGHUP, signal.SIGTERM):
            self.previous[signum] = signal.signal(signum, self.receive)
        return self

    def __exit__(self, *args):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)

    def receive(self, signum, _frame):
        # Popen must return an owned process before interruption can unwind.
        self.signum = self.signum or signum

    def check(self):
        if self.signum:
            raise Stopped(self.signum)

    def before_exec(self):
        for signum in self.previous:
            signal.signal(signum, signal.SIG_DFL)
        self.check()


def hook_trust(
    request: ProcessRequest,
    commands: dict[str, str],
    *,
    root=None,
    termination=None,
    listing=None,
) -> str:
    exe, args, cwd, env = request.exe, request.args, request.cwd, request.env
    listing = hooks_list if listing is None else listing
    root = sessions.store_directory(create=False, root=root)
    if root is None:
        raise StartError.missing_store()
    lock = os.path.join(
        root, hashlib.sha256(env["CODEX_HOME"].encode()).hexdigest() + ".start.lock"
    )
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise StartError.lock_access(lock, e) from e
    try:
        info = os.fstat(fd)
        if not sessions.private_file(info):
            raise StartError.unsafe_lock()
        _acquire_lock(fd, lock, termination)
        try:
            result = listing(exe, args, cwd, env, termination=termination)
        except app_server.Exited:
            time.sleep(RETRY)
            if termination is not None:
                termination.check()
            result = listing(exe, args, cwd, env, termination=termination)
    except app_server.Exited as e:
        raise StartError.early_exit(e) from None
    finally:
        os.close(fd)
    return trusted(result, cwd, commands)


def hooks_list(exe: str, args: list[str], cwd: str, env: dict[str, str], *, termination=None):
    try:
        return app_server.call(
            exe,
            args,
            "hooks/list",
            {"cwds": [cwd]},
            options=app_server.RpcOptions(
                env=env, wait=ANSWER_WAIT, limit=ANSWER_MAX, cwd=cwd, termination=termination
            ),
        )
    except app_server.Error as e:
        raise StartError.listing_failed(e) from None


def trusted(result, cwd: str, commands: dict[str, str]) -> str:
    hooks = _listed_hooks(result, cwd)
    ours = [h for h in hooks if h.get("source") == "sessionFlags"]
    trusted = []
    for event, name, _ in HOOKS:
        command = commands[event]
        rows = [h for h in ours if h.get("eventName") == name]
        if len(rows) != 1:
            raise StartError.hook_count(name, len(rows))
        h = rows[0]
        if StartError._hook_problem(h, command):
            raise StartError.invalid_hook(name, h, command)
        trusted.append(h)
    for h in ours:
        if not any(h is t for t in trusted):
            raise StartError.foreign_hook(h.get("key"))
    for h in hooks:
        if (
            h.get("source") != "sessionFlags"
            and h.get("enabled") is not False
            and h.get("isManaged") is not True
            and h.get("trustStatus") in ("untrusted", "modified")
        ):
            raise StartError.unreviewed_hook(h.get("key"), h["trustStatus"])
    return (
        "hooks.state={"
        + ",".join(
            f"{json.dumps(h['key'], ensure_ascii=False)}="
            f"{{trusted_hash={json.dumps(h['currentHash'])}}}"
            for h in trusted
        )
        + "}"
    )


def main(argv, *, trust=hook_trust):
    with _Termination() as termination:
        return _start(argv, termination, trust)


def _start(argv, termination, trust):
    data = _launch_data(argv)
    commands = data["commands"]
    sessions.absolute_path(data["store"], "store")
    state = sessions.load_session(data["workdir"], data["sid"], root=data["store"])
    if state is None:
        raise StartError.missing_state()
    env = {**os.environ, "CODEX_HOME": state.snapshot["codex_home"]}
    sessions.protect(
        state,
        [
            sys.prefix,
            sys.base_prefix,
            sys.executable,
            data["argv"][0],
            data["store"],
            *(word for word in data["hook"] if os.path.isabs(word)),
            *data["protect"],
        ],
        env,
        data["add_dirs"],
    )
    cwd = state.snapshot["cwd"]
    termination.check()
    trusted_state = trust(
        app_server.ProcessRequest(exe=data["argv"][0], args=data["hook_flags"], cwd=cwd, env=env),
        commands,
        root=data["store"],
        termination=termination,
    )
    command = data["argv"].copy()
    index = command.index("--") if "--" in command else len(command)
    command[index:index] = ["-c", trusted_state]
    os.chdir(cwd)
    termination.before_exec()
    os.execve(command[0], command, env)


def _acquire_lock(fd, lock, termination):
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        if termination is not None:
            termination.check()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise StartError.held_lock(lock, LOCK_WAIT) from None
            time.sleep(POLL)


def _listed_hooks(result, cwd):
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, list) else []
    if len(data) != 1:
        raise StartError.answer_count(cwd, len(data))
    entry = data[0] if isinstance(data[0], dict) else {}
    if entry.get("errors"):
        raise StartError.listing_errors(cwd, entry["errors"])
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or not all(isinstance(h, dict) for h in hooks):
        raise StartError.malformed_hooks(cwd)
    if not hooks:
        raise StartError.disabled_hooks()
    return hooks


def _launch_data(argv):
    if len(argv) != 1 or len(argv[0].encode("utf-8")) > ANSWER_MAX:
        raise StartError.oversized_launch()
    try:
        data = json.loads(argv[0])
    except (ValueError, RecursionError) as exc:
        raise StartError.malformed_launch() from exc
    if not isinstance(data, dict) or set(data) != {
        "argv",
        "hook",
        "commands",
        "hook_flags",
        "workdir",
        "sid",
        "store",
        "protect",
        "add_dirs",
    }:
        raise StartError.launch_fields()
    for key in ("argv", "hook", "hook_flags"):
        if (
            not isinstance(data[key], list)
            or not data[key]
            or any(not isinstance(w, str) or "\0" in w for w in data[key])
        ):
            raise StartError.launch_command()
    if not os.path.isabs(data["argv"][0]) or not os.path.isabs(data["hook"][0]):
        raise StartError.relative_executables()
    commands = data["commands"]
    if (
        not isinstance(commands, dict)
        or set(commands) != {event for event, name, _ in HOOKS}
        or any(not isinstance(word, str) or not word for word in commands.values())
    ):
        raise StartError.hook_commands()
    for key in ("protect", "add_dirs"):
        if not isinstance(data[key], list) or any(
            not isinstance(path, str) or "\0" in path or not os.path.isabs(path)
            for path in data[key]
        ):
            raise StartError.launch_paths(key)
    return data


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Stopped as exc:
        sys.exit(128 + exc.signum)
    except (StartError, sessions.RecoveryError, OSError, ValueError) as exc:
        print(f"codex tui: {exc}", file=sys.stderr)
        sys.exit(1)
