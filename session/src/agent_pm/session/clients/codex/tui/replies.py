"""Codex steer replies: a UserPromptSubmit while the session's turn is open is a steer; the first
assistant message after its user record in the session file is its answer, stored with save_reply
and announced as `reply <id>` in the events file, by a detached watcher (watch.py) or by the turn's
Stop or Interrupt hook, whichever finds it first. Each run's turn and pending steers are a state
file under the recovery store's replies/, read and written under its lock, never during a session
file read; the session file is client data (on the Docker Host, the container's), read only as a
regular file beneath / without following links. Scans start where the turn began, not where a
steer's hook ran (Docker's run late), and skip the turn's opening record and claimed ones.
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
CLAIMED_MAX = 64
PROMPT_MAX = 4096  # characters of a normalized prompt kept for matching
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
Clock = Callable[[], float]
Sleep = Callable[[float], None]


def state_path(store: str, workdir: str, sid: str) -> str:
    """The run's state file: its recovery file's name under the store's replies/."""
    recovery = SessionStore(store).file(workdir, sid)
    return os.path.join(os.path.dirname(recovery), "replies", os.path.basename(recovery))


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _normalized(text: str) -> str:
    return " ".join(text.split())[:PROMPT_MAX]


def _turn(*, open_: bool = False, turn_id=None, since=0.0, start=0, prompt="") -> dict:
    return {
        "open": open_,
        "turn_id": turn_id,
        "since": since,
        "start": start,  # the session file's size when the turn began
        "prompt": prompt,  # its opening prompt, normalized
        "opening": None,  # its opening user record's offset, once seen
        "claimed": [],  # steers' user record offsets
        "count": 0,  # steers so far
    }


def _fresh() -> dict:
    return {
        "version": 2,
        "name": "",
        "events": "",
        "store": "",
        "workdir": "",
        "sid": "",
        "native": "",
        "rollout": "",
        "turn": _turn(),
        "steers": [],
    }


def _offset(value) -> bool:
    return type(value) is int and value >= 0


def _steer_ok(steer) -> bool:
    return (
        isinstance(steer, dict)
        and set(steer) == {"n", "prompt", "cursor", "at", "skip"}
        and isinstance(steer["prompt"], str)
        and _offset(steer["n"])
        and _offset(steer["cursor"])
        and (steer["at"] is None or _offset(steer["at"]))
        and isinstance(steer["skip"], bool)
    )


def _turn_ok(turn) -> bool:
    return (
        isinstance(turn, dict)
        and set(turn) == set(_turn())
        and isinstance(turn["open"], bool)
        and (turn["turn_id"] is None or isinstance(turn["turn_id"], str))
        and isinstance(turn["since"], int | float)
        and _offset(turn["start"])
        and isinstance(turn["prompt"], str)
        and (turn["opening"] is None or _offset(turn["opening"]))
        and isinstance(turn["claimed"], list)
        and all(map(_offset, turn["claimed"]))
        and _offset(turn["count"])
    )


def _valid(state) -> bool:
    fresh = _fresh()
    if not isinstance(state, dict) or set(state) != set(fresh) or state["version"] != 2:
        return False
    texts = [key for key in fresh if key not in ("version", "turn", "steers")]
    return (
        all(isinstance(state[key], str) for key in texts)
        and _turn_ok(state["turn"])
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
    """Drops state files older than STALE_AGE with their lock files (drop_stale would also drop a
    live run's old lock file).
    """
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
def _locked(path: str, deadline: float, clock: Clock, sleep: Sleep):
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
    """Codex's prompt record: a user message whose text is the hook's prompt (any, without one)."""
    found = _normalized(text)
    return not prompt or found == prompt


def scan(state: dict, *, final: str = "", budget: int) -> dict[str, Any]:
    """The turn's and each pending steer's progress through the session file, `budget` bytes in
    all: {"opening", "claimed", "steers": {n: {cursor, at, skip, answer}}}; an answer is None until
    found, and `final` for a steer whose user record has none.
    """
    turn = state["turn"]
    opening, claimed, steers = turn["opening"], set(turn["claimed"]), {}
    for steer in state["steers"]:
        found = steers[steer["n"]] = {**steer, "answer": None}
        start = max(turn["start"], steer["cursor"])
        limit = min(SCAN_MAX, budget)
        data = read_beneath("/", state["rollout"], offset=start, limit=limit) if limit else None
        if not data:
            continue
        budget -= len(data)
        complete = data[: data.rfind(b"\n") + 1]
        if not complete:
            if len(data) == SCAN_MAX:  # one line longer than a read: skip past it
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
            if role == "user":
                if opening is None and _matches(turn["prompt"], text):
                    opening = here
                elif (
                    found["at"] is None
                    and here != opening
                    and here not in claimed
                    and _matches(steer["prompt"], text)
                ):
                    found["at"] = here
                    claimed.add(here)
            elif found["at"] is not None and text.strip():
                found["answer"] = text
                break
        found["cursor"] = position
    for found in steers.values():
        if found["answer"] is None and found["at"] is not None and final.strip():
            found["answer"] = final
    return {"opening": opening, "claimed": sorted(claimed), "steers": steers}


def _emit(state: dict, at: int, text: str) -> None:
    """Stores the answer once per steer record, then appends its `reply <id>` line."""
    reply_id = hashlib.sha256(f"{state['native']}:{at}".encode()).hexdigest()[:32]
    if save_reply(state["events"], reply_id, text):
        SessionStatusLog(state["events"]).write(state["name"], "reply", reply_id)


def _commit(current: dict, progress: dict, *, close: bool) -> None:
    """Applies a scan's progress to the turn it scanned: emits each answer (a failure said on
    stderr, its steer dropped), keeps the rest pending; `close` closes the turn.
    """
    turn = current["turn"]
    if turn["opening"] is None:
        turn["opening"] = progress["opening"]
    turn["claimed"] = sorted(set(turn["claimed"]) | set(progress["claimed"]))[-CLAIMED_MAX:]
    kept = []
    for steer in current["steers"]:
        found = progress["steers"].get(steer["n"])
        if found is None:
            kept.append(steer)
            continue
        steer.update(cursor=found["cursor"], at=found["at"], skip=found["skip"])
        if found["answer"] is None:
            kept.append(steer)
            continue
        try:
            _emit(current, found["at"], found["answer"])
        except (OSError, ValueError) as error:
            print(f"codex reply: {error}", file=sys.stderr)
    current["steers"] = kept
    if close:
        turn["open"], current["steers"] = False, []


def settle(
    path: str,
    *,
    deadline: float,
    budget: int,
    clock: Clock,
    sleep: Sleep,
    final: str = "",
    close: bool = False,
    resolve: bool = True,
) -> int:
    """Scans for pending steers' answers, then, locked again, commits what the scan found for the
    same turn (and, `close`, closes it); the pending steers left.
    """
    with _locked(path, deadline, clock, sleep):
        state = _load(path)
    if not state["turn"]["open"]:
        return 0
    if resolve:
        _resolve(state)
    progress = scan(state, final=final, budget=budget) if state["rollout"] else None
    with _locked(path, deadline, clock, sleep):
        current = _load(path)
        if current["turn"] == state["turn"]:
            if not current["rollout"]:
                current.update(native=state["native"], rollout=state["rollout"])
            _commit(
                current, progress or {"opening": None, "claimed": [], "steers": {}}, close=close
            )
            save(path, json.dumps(current))
        return len(current["steers"]) if current["turn"]["open"] else 0


def ensure_watcher(path: str) -> None:
    """Starts a detached watcher of `path` unless one holds its watch lock."""
    try:
        with file_lock(path + ".watch", wait=False):
            pass
    except BlockingIOError:
        return
    subprocess.Popen(  # detached: never waited for
        [sys.executable, "-P", "-s", "-m", WATCH, path],
        start_new_session=True,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, **source_path(module_roots(sys.modules[__name__], agent_pm.utils))},
        cwd="/",
    )


def watch(path: str, *, clock: Clock | None = None, sleep: Sleep | None = None) -> int:
    """The watcher: settles every POLL s while steers are pending; ends IDLE_EXIT s after none
    are, once the session file has not grown for QUIET s, or after LIFETIME s; 0 at once while
    another watcher holds the watch lock.
    """
    try:
        with file_lock(path + ".watch", wait=False):
            return _watch(path, clock or time.monotonic, sleep or time.sleep)
    except BlockingIOError:
        return 0


def _watch(path: str, clock: Clock, sleep: Sleep) -> int:
    began = idle = grew = clock()
    size = -1
    while clock() - began < LIFETIME:
        try:
            deadline = clock() + WATCH_LOCK_WAIT
            pending = settle(
                path, deadline=deadline, budget=HOOK_SCAN_MAX, clock=clock, sleep=sleep
            )
        except (TimeoutError, OSError) as error:
            print(f"codex reply: {error}", file=sys.stderr)
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


def on_hook(call: HookCall, *, began: float) -> None:
    """The Codex client hook's steer bookkeeping, within its deadline from `began` (the hook's
    time.monotonic() start); never raises, failures said on stderr.
    """
    try:
        _on_hook(call, began + DEADLINES.get(call.label, DEADLINE))
    except Exception as error:  # noqa: BLE001  # steer replies must never fail the hook or keep its done line
        print(f"codex reply: {error}", file=sys.stderr)


def _on_hook(call: HookCall, deadline: float) -> None:
    name, events, store = call.state.get("name"), call.events, call.state.get("store")
    if call.label not in LABELS or not (isinstance(name, str) and name and events):
        return
    if not isinstance(store, str):
        return
    clock, sleep, payload = time.monotonic, time.sleep, call.payload
    path = state_path(store, call.workdir, call.sid)
    _directory(os.path.dirname(path))
    _prune(os.path.dirname(path))
    if call.label in ("Stop", "Interrupt"):
        final = _text(payload.get("last_assistant_message")) if call.label == "Stop" else ""
        settle(
            path,
            deadline=deadline,
            budget=HOOK_SCAN_MAX,
            clock=clock,
            sleep=sleep,
            final=final,
            close=True,
            resolve=False,
        )
        return
    with _locked(path, deadline, clock, sleep):
        state = _load(path)
    state.update(name=name, events=events, store=store, workdir=call.workdir, sid=call.sid)
    _resolve(state)  # walks the client's sessions tree: never under the lock
    turn_id, now = _text(payload.get("turn_id")) or None, time.time()
    prompt = _normalized(_text(payload.get("prompt")))
    with _locked(path, deadline, clock, sleep):
        current = _load(path)
        current.update(name=name, events=events, store=store, workdir=call.workdir, sid=call.sid)
        if not current["rollout"]:
            current.update(native=state["native"], rollout=state["rollout"])
        turn = current["turn"]
        steered = call.label == "UserPromptSubmit" and _steer(turn, turn_id, now)
        if call.label == "SessionStart":
            current["turn"], current["steers"] = _turn(), []
        elif steered:
            steer = {"n": turn["count"], "prompt": prompt, "cursor": 0, "at": None, "skip": False}
            turn["count"] += 1
            current["steers"] = [*current["steers"], steer][-PENDING_MAX:]
        else:
            start = _size(current["rollout"]) if current["rollout"] else 0
            current["turn"] = _turn(
                open_=True, turn_id=turn_id, since=now, start=start, prompt=prompt
            )
            current["steers"] = []
        save(path, json.dumps(current))
    if steered:
        ensure_watcher(path)
