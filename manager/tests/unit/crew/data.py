"""Shared data fixtures for this topic."""

import contextlib
import datetime
import fcntl
import json
import os
import shlex
import signal
from dataclasses import dataclass
from unittest import mock

from agent_pm.manager.engine import crew, roster
from agent_pm.manager.engine.types import Spec
from agent_pm.session import (
    SessionRow,
)
from agent_pm.utils import LOCAL

from manager.tests.case import PYTHONPATH, SRC

CREW = os.path.join(SRC["manager"], "agent_pm", "manager", "engine", "crew.py")


MODULE = ["-P", "-m", "agent_pm.manager.engine.crew"]


SID = "0b6f1c2e-3d4a-4b5c-8d6e-7f8091a2b3c4"


SID2 = "1" + SID[1:]


WHEN = "2026-10-09T12:00:00+00:00"


PY = "/usr/bin/python3"


SCRIPT = {"worker": "/x/crew.py", "role": "/x/drive.py", "pipeline": "/x/router.py"}


HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tpane\n"


UNPRINTABLE = "want printable text (no control character, U+2028 or U+2029)"


DEAD_TEXT = "Error: boom\n\n  \n" + "\n" * 10 + "Pane is dead (status 1)\n"


DEAD_MSG = "w1: rt0 exited 1 at once; its pane's last lines:\nError: boom\nPane is dead (status 1)"


MGR = {"session": "mgr", "since": WHEN}


OTHER = {"session": "other", "since": WHEN}


LOCAL = os.path.basename(LOCAL)


WIDTH = 64


SPAN = roster.EVENTS_MAX // WIDTH


@contextlib.contextmanager
def fixed_time(text):
    instant = datetime.datetime.fromisoformat(text)
    with mock.patch.object(datetime, "datetime", wraps=datetime.datetime) as clock:
        clock.now.return_value = instant
        yield


def nl(*lines):
    return "".join(f"{x}\n" for x in lines)


def lock_free(path):
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        return True
    finally:
        os.close(fd)


@contextlib.contextmanager
def within(seconds=10):
    def fire(*_):
        raise AssertionError("blocked")

    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@dataclass(frozen=True, kw_only=True)
class SessionView:
    state: str = "working"
    pane: str | None = None
    opener: str | None = None
    shown: str | None = None
    runtime: str | None = None
    last_lines: str | None = None


def row(name, sid=None, *, view: SessionView | None = None):
    view = SessionView() if view is None else view
    return SessionRow(
        name,
        sid,
        view.runtime,
        view.state,
        view.pane,
        view.opener,
        view.shown,
        view.last_lines,
        own=False,
    )


def start(name, directory, *, own, **kw):
    place = {k: kw.pop(k) for k in ("own_runtime", "driver") if k in kw}
    return crew.start(Spec(name, **kw), directory=directory, own=own, **place)


def started(name, *rest):
    """A worker entry's resume: crew run as a module."""
    return [PY, *MODULE, "start", name, *rest]


def recovered(argv):
    """A crew module's recovery as attach prints it."""
    return f"env PYTHONPATH={shlex.quote(PYTHONPATH)} {shlex.join(argv)}"


def numbered(count, start=1):
    return nl(*(f"12:00:00 w{i} done ".ljust(WIDTH - 1, ".") for i in range(start, start + count)))


def tail(directory, n=0, gen=0, *backlog):
    me, events = (
        f"env PYTHONPATH={shlex.quote(PYTHONPATH)} python3 {' '.join(MODULE)}",
        shlex.quote(os.path.join(directory, "events")),
    )
    name = os.path.basename(directory)
    return nl(
        *backlog,
        f"N={n} GEN={gen}",
        f"monitor: tail -n +{n + 1} -F {events}",
        f"monitor expired: {me} attach --manager {name} --after LINE --gen {gen} --resume",
        f"next-event: {me} next-event --manager {name} --after {n} --gen {gen}",
    )


def entry(kind="worker", **over):
    return {
        "kind": kind,
        "sid": SID,
        "cwd": "/w",
        "resume": [PY, SCRIPT[kind], "--go"],
        "note": None,
        "opener": None,
        "pane": None,
        "split": None,
        "split_from": None,
        "tui": None,
        "state": "working",
        "started": WHEN,
        **over,
    }


def result(**fields):
    return json.dumps({"ts": "2026-10-09T12:00:00", "kind": "result", **fields}) + "\n"


def stored_entry(directory, name, value):
    with roster.locked(directory) as record:
        roster.record(record, name, value)
