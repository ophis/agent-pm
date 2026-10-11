"""The command each hook of a crew.py Codex worker runs (clients/_codex_worker.py), outside Codex's sandbox:
`_codex_hook.py <word> <events> <record> <tmux>`, <word> one of WORDS. Reads the hook's JSON payload on stdin to its
end; binds the worker record to the payload's session; for done (Stop) saves its last_assistant_message as the worker's
reply; then, whatever those did, marks the pane (tmux_host.mark). Never prints on stdout (Codex reads a SessionStart's
or UserPromptSubmit's stdout as model context) and always exits 0 (exit 2 denies or blocks); diagnostics go to stderr.
No payload text reaches an event line or a tmux argument. Stdlib, and tmux_host from core's src/."""
import fcntl
import json
import os
import stat
import sys
import tempfile
import time
import uuid

WORDS = {"session": "working", "working": "working", "blocked": "blocked", "done": "done", "interrupt": "done"}
VERSION = 1
KEYS = frozenset({"version", "sid", "cwd", "codex_home", "native"})
RECORD_MAX = 64 << 10   # bytes in a worker record
PAYLOAD_MAX = 64 << 20  # bytes of a payload parsed, and of a reply read
BLOCK = 1 << 16
LOCK_WAIT = 5   # seconds to get the record's lock
POLL = 0.05


def canonical(text) -> bool:
    """Whether text is a UUID as Codex writes one: lowercase, hyphenated."""
    try:
        return isinstance(text, str) and str(uuid.UUID(text)) == text
    except ValueError:
        return False


def unsafe(fd: int, limit: int) -> str:
    """Why the file open as fd is no file of ours to trust ("" when it is): a 0600 regular file of the caller's, at
    most limit bytes."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        return "not a 0600 regular file of yours"
    return f"over {limit} bytes" if st.st_size > limit else ""


def load(data: bytes, sid: str) -> dict | None:
    """A worker record's fields: exactly KEYS, version VERSION, sid the record's, absolute cwd and codex_home, native
    null or canonical; None when data is not that."""
    try:
        record = json.loads(data)
    except (ValueError, RecursionError):
        return None
    if not (isinstance(record, dict) and set(record) == KEYS and type(record["version"]) is int
            and record["version"] == VERSION and record["sid"] == sid
            and all(isinstance(record[k], str) and os.path.isabs(record[k]) for k in ("cwd", "codex_home"))
            and (record["native"] is None or canonical(record["native"]))):
        return None
    return record


def dump(record: dict) -> bytes:
    return json.dumps(record).encode()


def reply_path(record: str) -> str:
    """The reply file beside a worker record <sid>.json: <sid>.reply."""
    return record[:-len(".json")] + ".reply"


def main(argv: list[str]) -> None:
    payload = _payload(sys.stdin.buffer)
    if (len(argv) != 4 or argv[0] not in WORDS or not argv[2].endswith(".json")
            or not all(os.path.isabs(a) for a in argv[1:])):
        print(f"codex_hook: usage: _codex_hook.py {{{','.join(WORDS)}}} EVENTS RECORD.json TMUX", file=sys.stderr)
        return
    word, events, record, tmux = argv
    try:
        _bind(word, payload, record)
    except (OSError, ValueError) as e:
        print(f"codex_hook: {record}: {e}", file=sys.stderr)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import tmux_host
    try:
        tmux_host.mark(WORDS[word], events, tmux=tmux)
    except tmux_host.TuiError as e:
        print(f"codex_hook: {e}", file=sys.stderr)


def _payload(stream) -> dict:
    """The payload read to its end (Codex writes it whole, stalling on a hook that stops reading); {} when over
    PAYLOAD_MAX bytes or not a JSON object."""
    chunks, size = [], 0
    while chunk := stream.read(BLOCK):
        size += len(chunk)
        if size <= PAYLOAD_MAX:
            chunks.append(chunk)
    if size > PAYLOAD_MAX:
        return {}
    try:
        payload = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _bind(word: str, payload: dict, record: str) -> None:
    """A root session's payload (no agent_id, a canonical session_id, its cwd the record's): the record, unbound,
    gets its session_id as native, rewritten in place under its lock; bound to another, it stays (stderr). Bound to it
    and done: the payload's last_assistant_message, a string, becomes the reply."""
    session, cwd = payload.get("session_id"), payload.get("cwd")
    if payload.get("agent_id") is not None or not canonical(session) or not isinstance(cwd, str):
        return
    fd = os.open(record, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if why := unsafe(fd, RECORD_MAX):
            raise ValueError(why)
        _lock(fd)
        fields = load(os.pread(fd, RECORD_MAX + 1, 0), os.path.basename(record)[:-len(".json")])
        if fields is None:
            raise ValueError("malformed worker record")
        if os.path.realpath(cwd) != fields["cwd"]:
            return
        if fields["native"] is None:
            fields["native"] = session
            data = dump(fields)
            if os.pwrite(fd, data, 0) != len(data):
                raise ValueError("short write")
            os.ftruncate(fd, len(data))
            os.fsync(fd)
        elif fields["native"] != session:
            print(f"codex_hook: {record}: bound to {fields['native']}, not {session}", file=sys.stderr)
            return
        message = payload.get("last_assistant_message")
        if word == "done" and isinstance(message, str):
            _reply(record, message)
    finally:
        os.close(fd)


def _lock(fd: int) -> None:
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise ValueError(f"locked for {LOCK_WAIT} s") from None
            time.sleep(POLL)


def _reply(record: str, text: str) -> None:
    """text, as UTF-8, in the record's reply file: written to a temp file in its dir, then moved over it."""
    fd, tmp = tempfile.mkstemp(prefix=".reply-", dir=os.path.dirname(record))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8", "replace"))
        os.replace(tmp, reply_path(record))
    except BaseException:
        os.unlink(tmp)
        raise


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:   # Codex reads any other exit as the hook's failure (exit 2: a denial)
        print(f"codex_hook: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(0)
