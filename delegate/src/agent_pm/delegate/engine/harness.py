"""The detached Delegate process, loaded from its authenticated per-launch invocation.
Its launch (HarnessDelegate's, or the deprecated Driver.detach) blocks SIGNALS through exec; main
unblocks them after installing its handlers.
"""

import hashlib
import hmac
import json
import os
import re
import signal
import stat
import sys
from types import SimpleNamespace

from agent_pm.delegate.cli import spec_of
from agent_pm.delegate.engine import detach
from agent_pm.delegate.engine.application import Application
from agent_pm.delegate.engine.types import Stopped
from agent_pm.session import SIGNALS
from agent_pm.utils import sid_error

STATE_NAME = re.compile(r"harness-([0-9a-f]{64})-[a-z0-9_]{8}\.json")
INVOCATION_KEYS = frozenset(
    {
        "harness",
        "role",
        "task",
        "input",
        "out",
        "workdir",
        "client",
        "mode",
        "sid",
        "events",
        "split",
        "split_from",
        "opener",
        "repo",
        "resume",
        "detach",
        "dry_run",
        "list",
    }
)


def main(argv: list[str], *, application=None) -> int:
    if len(argv) != 1:
        print("usage: delegate harness WORKDIR", file=sys.stderr)
        return 2
    try:
        invocation = _invocation(argv[0])
    except (OSError, ValueError) as error:
        print(f"delegate: harness state: {error}", file=sys.stderr)
        return 2
    status = None
    try:
        for signum in SIGNALS:
            signal.signal(signum, _stop)
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
            owner = Application() if application is None else application
            code, status = owner.run_cli(invocation, spec_of(invocation, invocation.input))
            return code
        finally:
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    except Stopped as stopped:
        return stopped.code
    finally:
        detach.outcome(invocation.events, invocation.harness, status)


def _invocation(workdir: str) -> SimpleNamespace:
    basename = os.environ.pop("AGENT_PM_HARNESS_STATE", "")
    digest = os.environ.pop("AGENT_PM_HARNESS_DIGEST", "")
    matched = STATE_NAME.fullmatch(basename)
    if matched is None or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("missing or invalid launch credentials")
    path = os.path.join(workdir, basename)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("invocation must be a user-owned regular file with mode 0600")
        payload = stream.read()
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), digest):
        raise ValueError("invocation does not match its launch digest")
    given = json.loads(payload.decode("utf-8"))
    _validate(given, workdir, matched[1])
    os.unlink(path)
    return SimpleNamespace(**given)


def _validate(given, workdir: str, sid_hash: str) -> None:
    if not isinstance(given, dict) or given.keys() != INVOCATION_KEYS:
        raise ValueError("invalid invocation fields")
    for key in ("resume", "detach", "dry_run", "list"):
        if type(given[key]) is not bool or (key != "resume" and given[key]):
            raise ValueError(f"invalid {key}")
    sid = given["sid"]
    if error := sid_error(sid):
        raise ValueError(error)
    if hashlib.sha256(sid.encode("utf-8")).hexdigest() != sid_hash:
        raise ValueError("invocation sid does not match its filename")
    for key in ("role", "client"):
        if not _text(given[key]):
            raise ValueError(f"invalid {key}")
    if given["mode"] not in ("headless", "tui"):
        raise ValueError("invalid mode")
    if not isinstance(given["input"], str) or "\0" in given["input"]:
        raise ValueError("invalid input")
    for key in ("events", "out", "workdir"):
        if not _text(given[key]) or not os.path.isabs(given[key]):
            raise ValueError(f"invalid {key}")
    if os.path.realpath(given["workdir"]) != os.path.realpath(workdir):
        raise ValueError("invocation workdir does not match its launch directory")
    for key in ("task", "repo", "split", "split_from", "opener"):
        if given[key] is not None and not _text(given[key]):
            raise ValueError(f"invalid {key}")
    if given["harness"] != detach.names(given["role"], sid, given["mode"])[1]:
        raise ValueError("invalid harness name")


def _text(value) -> bool:
    return isinstance(value, str) and bool(value) and value.isprintable()


def _stop(signum, _frame):
    signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    raise Stopped(128 + signum)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
