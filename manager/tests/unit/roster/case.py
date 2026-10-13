"""Shared case fixtures for this topic."""

import contextlib
import fcntl
import io
import json
import os
import re
import types
import unittest
import unittest.mock

from agent_pm.manager.engine import roster
from agent_pm.session import DriverError
from agent_pm.utils import LOCAL

from manager.tests.case import Case
from manager.tests.unit.roster.data import row


class BusySession(DriverError):
    def __init__(self, name):
        super().__init__(f"cannot close tui session {name}: tmux: busy")


class Tmux:
    def __init__(self):
        self.alive, self.stuck, self.own, self.error, self.calls = set(), set(), None, None, []

    def sessions(self, match=None):
        self.calls.append(("sessions", match))
        if self.error:
            raise self.error
        names = sorted({*self.alive, *([self.own] if self.own else [])})
        return [row(n, own=n == self.own) for n in names if match is None or re.fullmatch(match, n)]

    def close(self, name):
        self.calls.append(("close", name))
        if self.error:
            raise self.error
        if name in self.stuck:
            raise BusySession(name)
        self.alive.discard(name)


class Base(Case):
    def setUp(self):
        self.managers = os.path.join(self.agent_pm, "managers")
        self.dir = os.path.join(self.managers, "m1")
        self.tmux = Tmux()
        self.driver = self.tmux

    def umask(self, mask):
        self.addCleanup(os.umask, os.umask(mask))

    def outside(self, name):
        path = os.path.join(os.path.dirname(self.agent_pm), name)
        os.mkdir(path)
        return path

    def local(self, text):
        with open(os.path.join(self.agent_pm, os.path.basename(LOCAL)), "w") as f:
            f.write(text)


class RosterCase(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "roster.json")
        self.lock = os.path.join(self.dir, "roster.lock")
        roster.ensure(self.dir)

    def doc(self, **top):
        return {"version": 1, "holder": None, "cursor": 0, "gen": 0, "entries": {}, **top}

    def store(self, data):
        raw = (
            data
            if isinstance(data, bytes)
            else data.encode()
            if isinstance(data, str)
            else json.dumps(data).encode()
        )
        with open(self.path, "wb") as f:
            f.write(raw)
        os.chmod(self.path, 0o600)
        return raw

    def bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def entries(self):
        return json.loads(self.bytes())["entries"]

    def load(self, **kw):
        err = io.StringIO()
        with (
            contextlib.redirect_stderr(err),
            roster.locked(self.dir, **{"write": False, **kw}) as r,
        ):
            pass
        return r, err.getvalue()

    def refused(self, message, **kw):
        with self.assertRaises(roster.RosterError) as cm, roster.locked(self.dir, **kw):
            pass
        self.assertEqual(str(cm.exception), message)

    def hold(self):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

    def foreign(self, path):
        ino, real = os.stat(path).st_ino, os.fstat

        def fstat(fd):
            st = real(fd)
            if st.st_ino != ino:
                return st
            return types.SimpleNamespace(
                st_mode=st.st_mode, st_uid=st.st_uid + 1, st_size=st.st_size
            )

        return unittest.mock.patch("os.fstat", fstat)
