"""Codex steer replies: a UserPromptSubmit while the session's turn is open is a steer; the first
assistant message after its user record in the session file is its answer, stored with save_reply
and announced as `reply <id>` in the events file, by a detached watcher (watch.py) or by the turn's
Stop or Interrupt hook, whichever finds it first. Each run's turn and pending steers are a state
file under the recovery store's replies/, read and written under its lock, never during a session
file read; the session file is client data (on the Docker Host, the container's), read only as a
regular file beneath / without following links.
"""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import agent_pm.utils
from agent_pm.session.clients.codex.common import (
    RecoveryError,
    SessionStore,
    session_file,
)
from agent_pm.session.clients.codex.tui import records
from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.engine.types import HookCall
from agent_pm.utils import (
    STALE_AGE,
    file_lock,
    json_records,
    module_roots,
    open_beneath,
    read_beneath,
    save,
    save_reply,
    source_path,
)

PENDING_MAX = 16
PROMPT_MAX = 4096  # characters of a steer's normalized prompt kept for matching
SCAN_MAX = 1 << 20  # bytes of the session file one steer's scan reads
HOOK_SCAN_MAX = 4 << 20  # bytes of the session file one scan reads for all steers
STATE_MAX = 1 << 20
DEADLINES = {"Interrupt": 1.5}  # seconds from the hook's start; its budget is 3 s, bind's 1 s
DEADLINE = 5.0  # other hooks', within their 10 s
LOCK_POLL = 0.05
WATCH_LOCK_WAIT = 1.0
POLL = 1.0
IDLE_EXIT = 2.0
LIFETIME = 6 * 3600  # a watcher's longest life, and an open turn's before it is stale
QUIET = 1800  # seconds the session file may stay the same size before a watcher gives up
LABELS = ("SessionStart", "UserPromptSubmit", "Stop", "Interrupt")
WATCH = f"{__package__}.watch"  # run as python -P -s -m on this layer's source roots


def state_path(store: str, workdir: str, sid: str) -> str:
    """The run's state file: its recovery file's name under the store's replies/."""
    recovery = SessionStore(store).file(workdir, sid)
    return os.path.join(os.path.dirname(recovery), "replies", os.path.basename(recovery))


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _fresh() -> dict:
    return {
        "version": 1,
        "name": "",
        "events": "",
        "store": "",
        "workdir": "",
        "sid": "",
        "native": "",
        "rollout": "",
        "turn": {"open": False, "turn_id": None, "since": 0.0},
        "steers": [],
    }


def _steer_ok(steer) -> bool:
    return (
        isinstance(steer, dict)
        and isinstance(steer.get("prompt"), str)
        and all(type(steer.get(key)) is int and steer[key] >= 0 for key in ("offset", "cursor"))
        and (steer.get("at") is None or type(steer["at"]) is int)
        and isinstance(steer.get("skip"), bool)
    )


def _valid(state) -> bool:
    fresh = _fresh()
    if not isinstance(state, dict) or set(state) != set(fresh) or state["version"] != 1:
        return False
    turn = state["turn"]
    return (
        all(
            isinstance(state[key], str) for key in fresh if key not in ("version", "turn", "steers")
        )
        and isinstance(turn, dict)
        and isinstance(turn.get("open"), bool)
        and (turn.get("turn_id") is None or isinstance(turn["turn_id"], str))
        and isinstance(turn.get("since"), int | float)
        and isinstance(state["steers"], list)
        and all(map(_steer_ok, state["steers"]))
    )


def _load(path: str) -> dict:
    """The state file's state; a fresh one when it is missing, refused or malformed."""
    data = read_beneath(os.path.dirname(path), path, limit=STATE_MAX + 1)
    try:
        state = json.loads(data) if data and len(data) <= STATE_MAX else None
    except (ValueError, RecursionError):
        state = None
    return state if isinstance(state, dict) and _valid(state) else _fresh()


def _directory(path: str) -> None:
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)


def _prune(directory: str) -> None:
    """Drops state files older than STALE_AGE with their lock files."""
    cutoff = time.time() - STALE_AGE
    with contextlib.suppress(OSError), os.scandir(directory) as entries:
        old = [
            entry.path
            for entry in entries
            if entry.name.endswith(".json")
            and entry.is_file(follow_symlinks=False)
            and entry.stat(follow_symlinks=False).st_mtime < cutoff
        ]
    for path in old:
        for name in (path, path + ".lock", path + ".watch.lock"):
            with contextlib.suppress(OSError):
                os.unlink(name)


@contextlib.contextmanager
def _locked(path: str, deadline: float, clock: Callable[[], float], sleep: Callable[[float], None]):
    """The state file's lock, waited for until `deadline`, then TimeoutError."""
    while True:
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(file_lock(path, wait=False))
            break
        except BlockingIOError:
            stack.close()
            if clock() >= deadline:
                raise TimeoutError(f"{path}: locked") from None
            sleep(LOCK_POLL)
    with stack:
        yield


def _size(path: str) -> int:
    try:
        fd = open_beneath("/", path)
    except OSError:
        return 0
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


def _resolve(state: dict) -> None:
    """The run's native id and session file, once its recovery state is bound."""
    if state["rollout"]:
        return
    try:
        recovery = SessionStore(state["store"]).load(state["workdir"], state["sid"])
        if recovery is None or recovery.native_id is None:
            return
        found = session_file(recovery.snapshot, recovery.native_id)
    except (RecoveryError, OSError, ValueError):
        return
    if found is not None:
        state["native"], state["rollout"] = recovery.native_id, found.path


def _steer(turn: dict, turn_id: str | None, now: float) -> bool:
    """Codex's steer rule: the open turn, fresh, and the same turn id unless either has none."""
    if not turn["open"] or now - turn["since"] > LIFETIME:
        return False
    return turn["turn_id"] is None or turn_id is None or turn["turn_id"] == turn_id


def _matches(prompt: str, text: str) -> bool:
    """Codex's steer record: a user message whose text is the hook's prompt (any, without one)."""
    found = _normalized(text)
    return not prompt or found == prompt or len(prompt) == PROMPT_MAX and found.startswith(prompt)


def _key(steer: dict) -> tuple[str, int]:
    return steer["prompt"], steer["offset"]


def scan(state: dict, *, final: str = "", budget: int) -> dict[tuple[str, int], dict[str, Any]]:
    """Each pending steer's progress through the session file, `budget` bytes in all: its cursor,
    `at` (its user record's offset) and `answer` (None: not yet; `final` once `at` has none).
    """
    progress: dict[tuple[str, int], dict[str, Any]] = {}
    for steer in state["steers"]:
        found: dict[str, Any] = {**steer, "answer": None}
        progress[_key(steer)] = found
        start = max(steer["offset"], steer["cursor"])
        limit = min(SCAN_MAX, budget)
        data = read_beneath("/", state["rollout"], offset=start, limit=limit) if limit else None
        if not data:
            continue
        budget -= len(data)
        complete = data[: data.rfind(b"\n") + 1]
        if not complete and len(data) == limit:  # one line longer than a read: skip past it
            found.update(cursor=start + len(data), skip=True)
            continue
        position = start
        for line in complete.split(b"\n")[:-1]:
            here, position = position, position + len(line) + 1
            if found["skip"]:
                found["skip"] = False
                continue
            said = next((records.message(r) for r in json_records([line])), None)
            if said is None:
                continue
            role, text = said
            if found["at"] is None:
                if role == "user" and _matches(steer["prompt"], text):
                    found["at"] = here
            elif role == "assistant" and text.strip():
                found["answer"] = text
                break
        found["cursor"] = position
    for found in progress.values():
        if found["answer"] is None and found["at"] is not None and final.strip():
            found["answer"] = final
    return progress


def _emit(state: dict, at: int, text: str) -> None:
    """Stores the answer once per steer record, then appends its `reply <id>` line."""
    reply_id = hashlib.sha256(f"{state['native']}:{at}".encode()).hexdigest()[:32]
    if save_reply(state["events"], reply_id, text):
        SessionStatusLog(state["events"]).write(state["name"], "reply", reply_id)


def settle(
    path: str,
    *,
    deadline: float,
    budget: int,
    final: str = "",
    close: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Scans for pending steers' answers, then, locked again, emits the ones still pending (and,
    `close`, closes the turn); the pending steers left.
    """
    with _locked(path, deadline, clock, sleep):
        state = _load(path)
    if not state["turn"]["open"]:
        return 0
    _resolve(state)
    progress = scan(state, final=final, budget=budget) if state["rollout"] else {}
    with _locked(path, deadline, clock, sleep):
        current = _load(path)
        same = current["turn"] == state["turn"]
        if same and not current["rollout"]:
            current.update(native=state["native"], rollout=state["rollout"])
        kept = []
        for steer in current["steers"] if same else ():
            found = progress.get(_key(steer))
            if found is not None:
                steer.update(cursor=found["cursor"], at=found["at"], skip=found["skip"])
                if found["answer"] is not None:
                    _emit(current, found["at"], found["answer"])
                    continue
            kept.append(steer)
        if same:
            current["steers"] = kept
        if close and same:
            current["turn"]["open"], current["steers"] = False, []
        if same:
            save(path, json.dumps(current))
        return len(current["steers"]) if current["turn"]["open"] else 0


def ensure_watcher(path: str) -> None:
    """Starts a detached watcher of `path` unless one holds its watch lock."""
    try:
        with file_lock(path + ".watch", wait=False):
            pass
    except BlockingIOError:
        return
    env = {
        **os.environ,
        **source_path(module_roots(sys.modules[__name__], agent_pm.utils)),
    }
    subprocess.Popen(  # detached: never waited for
        [sys.executable, "-P", "-s", "-m", WATCH, path],
        env=env,
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def watch(
    path: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """The watcher: settles every POLL s while steers are pending; ends IDLE_EXIT s after none
    are, once the session file has not grown for QUIET s, or after LIFETIME s; 0 at once while
    another watcher holds the watch lock.
    """
    try:
        with file_lock(path + ".watch", wait=False):
            return _watch(path, clock, sleep)
    except BlockingIOError:
        return 0


def _watch(path: str, clock, sleep) -> int:
    began = idle = grew = clock()
    size = -1
    while clock() - began < LIFETIME:
        try:
            pending = settle(
                path,
                deadline=clock() + WATCH_LOCK_WAIT,
                budget=HOOK_SCAN_MAX,
                clock=clock,
                sleep=sleep,
            )
        except TimeoutError:
            pending = 1
        if pending:
            idle = clock()
        elif clock() - idle >= IDLE_EXIT:
            return 0
        found = _size(_load(path)["rollout"])
        if found != size:
            size, grew = found, clock()
        elif clock() - grew >= QUIET:
            return 0
        sleep(POLL)
    return 0


def on_hook(call: HookCall, payload: dict) -> None:
    """The Codex client hook's steer bookkeeping; never raises, failures said on stderr."""
    try:
        _on_hook(call, payload)
    except Exception as error:  # noqa: BLE001  # steer replies must never fail the hook or keep its done line
        print(f"codex reply: {error}", file=sys.stderr)


def _on_hook(call: HookCall, payload: dict) -> None:
    name, events, store = call.state.get("name"), call.events, call.state.get("store")
    if call.label not in LABELS or not isinstance(name, str) or not name or not events:
        return
    if not isinstance(store, str):
        return
    deadline = time.monotonic() + DEADLINES.get(call.label, DEADLINE)
    path = state_path(store, call.workdir, call.sid)
    _directory(os.path.dirname(path))
    _prune(os.path.dirname(path))
    if call.label in ("Stop", "Interrupt"):
        final = _text(payload.get("last_assistant_message")) if call.label == "Stop" else ""
        settle(path, deadline=deadline, budget=HOOK_SCAN_MAX, final=final, close=True)
        return
    spawn = False
    with _locked(path, deadline, time.monotonic, time.sleep):
        state = _load(path)
        state.update(name=name, events=events, store=store, workdir=call.workdir, sid=call.sid)
        turn_id, now = _text(payload.get("turn_id")) or None, time.time()
        if call.label == "SessionStart":
            state["turn"], state["steers"] = {"open": False, "turn_id": None, "since": 0.0}, []
        elif _steer(state["turn"], turn_id, now):
            _resolve(state)
            prompt = _normalized(_text(payload.get("prompt")))[:PROMPT_MAX]
            offset = _size(state["rollout"]) if state["rollout"] else 0
            steer = {
                "prompt": prompt,
                "offset": offset,
                "cursor": offset,
                "at": None,
                "skip": False,
            }
            state["steers"] = [*state["steers"], steer][-PENDING_MAX:]
            spawn = True
        else:
            state["turn"], state["steers"] = {"open": True, "turn_id": turn_id, "since": now}, []
        save(path, json.dumps(state))
    if spawn:
        ensure_watcher(path)
