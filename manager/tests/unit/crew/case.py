"""Shared case fixtures for this topic."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import types
from unittest import mock

from agent_pm.manager.engine import crew, roster

from manager.tests.case import PYTHONPATH, Case
from manager.tests.unit.crew.data import CREW, LOCAL, WHEN, lock_free, start
from manager.tests.unit.crew.runtime import Api, Runs


class CrewCase(Case):
    """
    A caller-owned fake session driver, self.api (replaceable), sleep patched, stderr captured.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.api = Api(own=None)
        self.event_source = types.SimpleNamespace(next=crew.next_event)
        self.sleep = self.enterContext(mock.patch.object(time, "sleep"))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": PYTHONPATH}))
        self.local('runtime = "rt0"\n')

    def call(self, fn, /, *args, **kw):
        return getattr(self.api, fn)(*args, **kw)

    def local(self, text):
        with open(os.path.join(self.agent_pm, LOCAL), "w") as f:
            f.write(text)

    def made(self):
        return sorted(set(os.listdir(self.agent_pm)) - {LOCAL})

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = crew.main(list(argv), driver=self.api, event_source=self.event_source)
        return rc, out.getvalue(), err.getvalue()

    def main_with(self, fake, *argv):
        self.api = fake
        return self.run_main(*argv)

    def attached(self, *argv, own=None):
        saved = self.api
        try:
            rc, _, err = self.main_with(Api(own), "attach", *argv)
        finally:
            self.api = saved
        if rc:
            self.fail(f"attach exited {rc}: {err}")

    def record_of(self, sid):
        with open(os.path.join(self.agent_pm, "workers", sid, "worker.json")) as f:
            return json.load(f)


class WorkerCase(CrewCase):
    def setUp(self):
        super().setUp()
        self.directory = roster.directory("m1", driver=self.api)
        roster.ensure(self.directory)
        self.events = os.path.join(self.directory, "events")
        self.roster = os.path.join(self.directory, "roster.json")

    def started(self, name="w1", **kw):
        kw.setdefault("cwd", self.dir)
        return start(name, self.directory, own=None, driver=self.api, **kw)

    def entries(self):
        if not os.path.exists(self.roster):
            return {}
        with open(self.roster) as f:
            return json.load(f)["entries"]

    def protect(self):
        managers, workers = (os.path.join(self.agent_pm, d) for d in ("managers", "workers"))
        return tuple(map(os.path.realpath, (managers, workers, CREW)))

    def record(self, sid, **over):
        return {
            "version": 1,
            "name": "w1",
            "sid": sid,
            "runtime": "rt0",
            "cwd": self.dir,
            "flags": [],
            "env": {},
            "events": self.events,
            "manager": self.directory,
            "split": None,
            "split_from": None,
            **over,
        }


class RosterCase(CrewCase):
    def setUp(self):
        super().setUp()
        self.mdir = os.path.join(self.agent_pm, "managers", "mgr")
        self.path = os.path.join(self.mdir, "roster.json")
        self.lock = os.path.join(self.mdir, "roster.lock")
        self.api, self.runs = Api(), Runs()
        self.enterContext(
            mock.patch.object(subprocess, "run", lambda *a, **kw: self.runs(*a, **kw))
        )

    def seed(self, holder=None, cursor=0, gen=0, **entries):
        roster.ensure(self.mdir)
        with open(self.path, "w") as f:
            json.dump(
                {"version": 1, "holder": holder, "cursor": cursor, "gen": gen, "entries": entries},
                f,
            )
        return self.bytes()

    def bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def entries(self):
        return json.loads(self.bytes())["entries"]

    def held(self):
        return (
            f"workers: manager directory {self.mdir}: held by tmux session other since {WHEN}; "
            "ask that manager to "
            "run crew.py release, or end that session\n"
        )

    def not_attached(self):
        return f"workers: manager directory {self.mdir} not attached: run crew.py attach first\n"


class AttachCase(RosterCase):
    def setUp(self):
        super().setUp()
        self.shows, self.then = [], None

    def server(self, own="mgr", rows=()):
        fake = Api(own, rows)
        fake.placed = ("%9", "mgr")

        def shown(session):
            self.shows.append((session, lock_free(self.lock)))
            if self.then:
                self.then()

        fake.on_show = shown
        return fake

    def attach(self, fake, *argv):
        return self.main_with(fake, "attach", *argv)

    def synced(self, fake, *argv):
        rc, out, err = self.attach(fake, *argv)
        shown = [f"tui: session {s}: tmux attach -t '={s}'\n" for s, _, _ in fake.shows]
        self.assertEqual((rc, err), (0, "".join(shown)))
        return out
