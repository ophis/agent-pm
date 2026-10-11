"""CodexClient's worker capability: a crew.py worker is an interactive `codex` whose hooks run _codex_hook.py. Its
hooks are trusted for that invocation only, as one `codex app-server` hooks/list call lists them (hooks.state); its
worker record binds crew's session id to the Codex native ID the first hook reports. Pinned to codex 0.162.1: an
answer of another shape fails closed (core/CLAUDE.md › Codex workers)."""
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time

import roster
from compose import ConfigError

from . import _codex_hook, codex   # codex imports this module: use its names at call time
from .base import WorkerCommand, WorkerSpec

# (Config event, hooks/list eventName, helper word)
HOOKS = (("SessionStart", "sessionStart", "session"), ("UserPromptSubmit", "userPromptSubmit", "working"),
         ("PermissionRequest", "permissionRequest", "blocked"), ("Stop", "stop", "done"),
         ("Interrupt", "interrupt", "interrupt"))
# Notifications as BEL, not OSC 9 carrying the reply's text; the first -c, so a user's own after it wins.
NOTIFY = 'tui.notification_method="bel"'
HOOK = os.path.abspath(_codex_hook.__file__)
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # core's src/
HOOK_TIMEOUT = 10
INTERRUPT_TIMEOUT = 3   # Codex clamps an Interrupt hook's to 1-3 s
# Option names, any spelling: (-c X, -cX, -c=X, --config X, --config=X); each takes a value.
VALUED = {"-c": "-c", "--config": "-c", "-C": "-C", "--cd": "-C", "-p": "-p", "--profile": "-p",
          "--enable": "--enable", "--disable": "--disable", "--add-dir": "--add-dir"}
RESERVED = ("-C", "-p", "resume", "fork", "--last", "--dangerously-bypass-hook-trust")
RESERVED_KEYS = ("hooks", "sandbox_workspace_write")   # -c keys, with their subkeys, crew.py sets or keeps
PASSED = ("-c", "--enable", "--disable")   # the user's options app-server takes too
HASH = re.compile(r"sha256:[0-9a-f]{64}")
LOCK_WAIT = 30   # seconds for another start's hooks/list call on the same CODEX_HOME
ANSWER_WAIT = 20   # seconds for app-server's answers
EXIT_WAIT = 5   # seconds for app-server to exit once its stdin is closed
RETRY = 1   # seconds before the one retry of an app-server that exits before answering
POLL = 0.1
BLOCK = 1 << 16
ANSWER_MAX = 64 << 20   # bytes in one answer line


class _Exited(Exception):
    """app-server closed its stdout before answering."""


def worker(spec: WorkerSpec) -> WorkerCommand:
    """CodexClient.worker. New: a worker record, unbound, made first and left on a later failure. Resume: the record's
    native session, cwd and CODEX_HOME."""
    if not roster.SID.fullmatch(spec.sid):
        raise ConfigError(f"bad session id {spec.sid!r}")
    _refuse(spec.flags)
    exe = spec.exe or _which("codex", spec.env)
    python, hook, tmux = map(os.path.realpath, (sys.executable, HOOK, _which("tmux", spec.env)))
    _protect(spec, exe, tmux)
    if spec.resume:
        path, record = _record(spec.sid)
        if record["cwd"] != os.path.realpath(spec.cwd):
            raise ConfigError(f"--cwd {spec.cwd}: Codex worker {spec.sid} ran in {record['cwd']}")
        home = record["codex_home"]
        head = [exe, "resume", record["native"], "-C", spec.cwd, "--no-daemon"]
    else:
        home = os.path.realpath(codex._codex_home(spec.cwd, spec.env.get("CODEX_HOME") or "~/.codex"))
        path = os.path.join(_workers(create=True), f"{spec.sid}.json")
        _create(path, {"version": _codex_hook.VERSION, "sid": spec.sid, "cwd": os.path.realpath(spec.cwd),
                       "codex_home": home, "native": None})
        head = [exe, "--no-daemon", "-C", spec.cwd]
    commands = [shlex.join([python, "-I", hook, word, spec.events, path, tmux]) for _, _, word in HOOKS]
    hooks = [w for (event, _, _), command in zip(HOOKS, commands) for w in (
        "-c", f'hooks.{event}=[{{hooks=[{{type="command",command={json.dumps(command, ensure_ascii=False)},'
              f'timeout={INTERRUPT_TIMEOUT if event == "Interrupt" else HOOK_TIMEOUT}}}]}}]')]
    passed = [w for name, value, _ in _options(spec.flags) if name in PASSED for w in (name, value)]
    state = _hook_trust(exe, [*hooks, *passed], spec.cwd, {**spec.env, "CODEX_HOME": home}, commands)
    return WorkerCommand([*head, "-c", NOTIFY, *hooks, "-c", state, *spec.flags,
                          *(["--", spec.prompt] if spec.prompt else [])], {"CODEX_HOME": home})


def reply(sid: str) -> str:
    """CodexClient.worker_reply: the bound worker's reply file (_codex_hook.reply_path), "" when none yet."""
    path, _ = _record(sid)
    data = _read(_codex_hook.reply_path(path), _codex_hook.PAYLOAD_MAX, "Codex worker reply")
    return "" if data is None else data.decode("utf-8", "replace")


def _options(flags):
    """flags before a bare `--`: (name, value, as written) for a VALUED option, the name its short one; (word, None,
    flag) for any other, the word before a long option's `=`."""
    i = 0
    while i < len(flags) and flags[i] != "--":
        flag = flags[i]
        if flag.startswith("--"):
            word, eq, value = flag.partition("=")
        else:
            word, value = flag[:2], flag[2:].removeprefix("=")
            eq = flag[2:]
        name = VALUED.get(word)
        if name is None:
            yield (word if flag.startswith("--") else flag), None, flag
        elif eq:
            yield name, value, flag
        else:
            i += 1
            value = flags[i] if i < len(flags) else ""
            yield name, value, f"{flag} {value}"
        i += 1


def _refuse(flags) -> None:
    """Raises on a flag, before a bare `--`, that sets what crew.py sets: hooks and their trust, the session, the cwd
    or a profile (app-server cannot load one, so its hooks could not be checked); or the sandbox's writable roots."""
    for name, value, text in _options(flags):
        key = value.partition("=")[0].split(".", 1)[0].strip().strip("\"'") if name == "-c" else ""
        if name in RESERVED or key in RESERVED_KEYS:
            raise ConfigError(f"{text}: crew.py sets a Codex worker's hooks, session and cwd")


def _which(name: str, env) -> str:
    found = shutil.which(name, path=env.get("PATH"))
    if found is None:
        raise ConfigError(f"{name} not found on PATH")
    return os.path.abspath(found)


def _protect(spec: WorkerSpec, exe: str, tmux: str) -> None:
    """Raises when a root the worker may write, its cwd or an --add-dir (relative: to its cwd), holds or lies inside
    what its hooks run or write outside the sandbox, or codex itself (exe)."""
    roots = [os.path.realpath(spec.cwd), *(os.path.realpath(os.path.join(spec.cwd, value))
                                           for name, value, _ in _options(spec.flags) if name == "--add-dir")]
    paths = [os.path.realpath(p) for p in (SRC, sys.prefix, sys.base_prefix, sys.executable, tmux, exe,
                                           os.path.join(codex._store_root(), "workers"), spec.events)]
    for root in roots:
        for path in paths:
            if codex._inside(path, root) or codex._inside(root, path):
                raise ConfigError(f"Codex worker {root} could rewrite {path}, which its hooks run unsandboxed: use "
                                  "another --cwd")


def _workers(*, create: bool) -> str | None:
    """The worker record dir, <realpath ~>/.agent-pm/core-sessions/workers, and each dir to it checked
    (codex._private_dir); None: one is missing."""
    root = codex._store_root()
    for path, parent in ((os.path.dirname(root), True), (root, False), (os.path.join(root, "workers"), False)):
        try:
            if not codex._private_dir(path, create=create, config_parent=parent):
                return None
        except OSError as e:
            raise ConfigError(f"Codex worker record dir {path}: {e.strerror}") from e
    return path


def _create(path: str, record: dict) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as e:
        raise ConfigError(f"Codex worker record {path} already exists") from e
    except OSError as e:
        raise ConfigError(f"Codex worker record {path}: {e.strerror}") from e
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(_codex_hook.dump(record))
        f.flush()
        os.fsync(f.fileno())
    directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _record(sid: str) -> tuple[str, dict]:
    """sid's worker record, bound: its path and fields."""
    if not roster.SID.fullmatch(sid):
        raise ConfigError(f"bad session id {sid!r}")
    workers = _workers(create=False)
    path = os.path.join(workers, f"{sid}.json") if workers else None
    data = path and _read(path, _codex_hook.RECORD_MAX, "Codex worker record", shared=True)
    if data is None:
        raise ConfigError(f"no Codex worker record for {sid}")
    record = _codex_hook.load(data, sid)
    if record is None:
        raise ConfigError(f"malformed Codex worker record {path}")
    if record["native"] is None:
        raise ConfigError(f"Codex worker {sid} has no native session: it never took a prompt; start a new worker")
    return path, record


def _read(path: str, limit: int, what: str, *, shared: bool = False) -> bytes | None:
    """The file at path, a 0600 regular file of ours of at most limit bytes (_codex_hook.unsafe), never followed;
    None: missing. shared: read under a shared lock (the hook helper rewrites a record in place)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError(f"{what} {path}: {e.strerror}") from e
    with os.fdopen(fd, "rb") as f:
        if why := _codex_hook.unsafe(f.fileno(), limit):
            raise ConfigError(f"{what} {path}: {why}")
        if shared:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        return f.read(limit + 1)


def _hook_trust(exe: str, args: list[str], cwd: str, env: dict[str, str], commands: list[str]) -> str:
    """The -c arg trusting exactly the worker's hooks (args' hooks.* items, running commands in HOOKS order) for this
    invocation: hooks.state with each one's key and hash as `codex app-server <args>` (in cwd, with env) lists them
    for cwd. One such call per CODEX_HOME at a time (a cold home fails concurrent ones); one retry of an app-server
    that exits before answering. Fails closed (_trusted)."""
    lock = os.path.join(codex._store_root(), "workers",
                        hashlib.sha256(env["CODEX_HOME"].encode()).hexdigest()[:16] + ".lock")
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise ConfigError(f"codex hooks/list: {lock}: {e.strerror}") from e
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ConfigError(f"codex hooks/list: another Codex worker's start held {lock} for "
                                      f"{LOCK_WAIT} s") from None
                time.sleep(POLL)
        try:
            result = _hooks_list(exe, args, cwd, env)
        except _Exited:
            time.sleep(RETRY)
            result = _hooks_list(exe, args, cwd, env)
    except _Exited as e:
        raise ConfigError(f"codex hooks/list: app-server exited {e} before answering") from None
    finally:
        os.close(fd)
    return _trusted(result, cwd, commands)


def _hooks_list(exe: str, args: list[str], cwd: str, env: dict[str, str]):
    """hooks/list's result for cwd from a new `exe app-server` in its own process group: initialize, initialized,
    hooks/list, answers matched by id (notifications interleave), all within ANSWER_WAIT s. Then its stdin closed, it
    gets EXIT_WAIT s to exit; only then is its group killed."""
    try:
        proc = subprocess.Popen([exe, "app-server", *args], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise ConfigError(f"codex hooks/list: {exe}: {e.strerror}") from e
    lines = _Lines(proc.stdout.fileno(), time.monotonic() + ANSWER_WAIT)
    exited = False
    try:
        _send(proc, {"id": 1, "method": "initialize",
                     "params": {"clientInfo": {"name": "agent-pm", "version": "0"},
                                "capabilities": {"experimentalApi": True}}})
        _answer(lines, 1, "initialize")
        _send(proc, {"method": "initialized"})
        _send(proc, {"id": 2, "method": "hooks/list", "params": {"cwds": [cwd]}})
        return _answer(lines, 2, "hooks/list")
    except _Exited:
        exited = True
        raise
    finally:
        _close(proc)
        if exited:
            raise _Exited(proc.returncode)


def _send(proc, message: dict) -> None:
    try:
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()
    except BrokenPipeError:
        raise _Exited from None


class _Lines:
    """A pipe's lines, each read before a deadline."""

    def __init__(self, fd: int, deadline: float):
        self.fd, self.deadline, self.buf, self.seen = fd, deadline, bytearray(), 0

    def __next__(self) -> bytes:
        while (end := self.buf.find(b"\n", self.seen)) < 0:
            self.seen = len(self.buf)
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise ConfigError(f"codex hooks/list: no answer in {ANSWER_WAIT} s")
            if not select.select([self.fd], [], [], left)[0]:
                continue
            chunk = os.read(self.fd, BLOCK)
            if not chunk:
                raise _Exited
            if len(self.buf) + len(chunk) > ANSWER_MAX:
                raise ConfigError(f"codex hooks/list: an answer over {ANSWER_MAX} bytes")
            self.buf += chunk
        line = bytes(self.buf[:end])
        del self.buf[:end + 1]
        self.seen = 0
        return line


def _answer(lines: _Lines, ident: int, method: str):
    """The result of request `ident` (`method`); other lines skipped."""
    while True:
        try:
            message = json.loads(next(lines))
        except (ValueError, RecursionError):
            continue
        if not isinstance(message, dict) or "method" in message or type(message.get("id")) is not int \
                or message["id"] != ident:
            continue
        if "result" in message:
            return message["result"]
        error = message.get("error")
        raise ConfigError(f"codex hooks/list: {method} failed: "
                          f"{error.get('message') if isinstance(error, dict) else error}")


def _close(proc) -> None:
    """Close proc's stdin, read its stdout to the end and wait for its exit, EXIT_WAIT s in all; then kill its
    process group."""
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


def _trusted(result, cwd: str, commands: list[str]) -> str:
    """hooks.state={"<key>"={trusted_hash="<hash>"},…} for the HOOKS rows' entries of hooks/list's result, asked for
    cwd alone: its one entry, whatever path that gives (Codex may normalize it). Fails unless that entry has no errors
    and hooks (none: hooks are off, and turning them on would run the user's other hooks too); each row has exactly one
    sessionFlags entry, enabled, a command handler running its command, with a string key and a sha256 hash, and there
    is no other sessionFlags entry; and no other entry is enabled, unmanaged and untrusted or modified (its review
    dialog would invite a write to the user's Codex config)."""
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, list) else []
    if len(data) != 1:
        raise ConfigError(f"codex hooks/list: want one answer for {cwd}, got {len(data)}")
    entry = data[0] if isinstance(data[0], dict) else {}
    if entry.get("errors"):
        raise ConfigError(f"codex hooks/list: errors for {cwd}: {json.dumps(entry['errors'], ensure_ascii=False)}")
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or not all(isinstance(h, dict) for h in hooks):
        raise ConfigError(f"codex hooks/list: malformed hooks for {cwd}")
    if not hooks:
        raise ConfigError("codex hooks/list: Codex hooks are off (features.hooks); a Codex worker needs them")
    ours = [h for h in hooks if h.get("source") == "sessionFlags"]
    trusted = []
    for (_, name, _), command in zip(HOOKS, commands):
        rows = [h for h in ours if h.get("eventName") == name]
        if len(rows) != 1:
            raise ConfigError(f"codex hooks/list: {len(rows) or 'no'} session-flags {name} hook"
                              f"{'s' if len(rows) > 1 else ''}")
        h = rows[0]
        why = ("is disabled" if h.get("enabled") is not True else
               "is not our command" if h.get("handlerType") != "command" or h.get("command") != command else
               "has no key" if not isinstance(h.get("key"), str) else
               "has no sha256 hash" if not isinstance(h.get("currentHash"), str) or not HASH.fullmatch(
                   h["currentHash"]) else "")
        if why:
            raise ConfigError(f"codex hooks/list: the session-flags {name} hook {why}")
        trusted.append(h)
    for h in ours:
        if not any(h is t for t in trusted):
            raise ConfigError(f"codex hooks/list: a session-flags hook not ours: {h.get('key')}")
    for h in hooks:
        if (h.get("source") != "sessionFlags" and h.get("enabled") is not False and h.get("isManaged") is not True
                and h.get("trustStatus") in ("untrusted", "modified")):
            raise ConfigError(f"codex hooks/list: your hook {h.get('key')} is {h['trustStatus']}: review it in your "
                              "own Codex session (/hooks) first")
    return "hooks.state={" + ",".join(f"{json.dumps(h['key'], ensure_ascii=False)}="
                                      f"{{trusted_hash={json.dumps(h['currentHash'])}}}" for h in trusted) + "}"
