"""Shared runtime fixtures for this topic."""

import dataclasses
import re
import subprocess

from agent_pm.manager.engine import workers
from agent_pm.session import (
    DriverError,
    IdleTimeout,
    StartOptions,
)

from manager.tests.unit.crew.data import MODULE, SessionView, lock_free, row


class FakeDriverError(DriverError):
    @classmethod
    def close_failed(cls, name):
        return cls(f"cannot close tui session {name}: tmux: boom")

    @classmethod
    def no_tui(cls, name):
        return cls(f"no tui session {name}")


class Handle:
    def __init__(self, script):
        self.script, self.taken = script, 0

    def events(self):
        for event in self.script() if callable(self.script) else self.script:
            self.taken += 1
            if isinstance(event, Exception):
                raise event
            yield event


class Attached:
    def __init__(self, fake, name):
        self.fake, self.name = fake, name

    @property
    def reply(self):
        return self.fake.tuis[self.name][0]

    def screen(self, lines=None):
        self.fake.calls.append(("screen", self.name, lines))
        return self.fake.tuis[self.name][1]


class Api:
    """session.engine.api as crew calls it: rows by name (own: the caller's), start adding its row,
    every call recorded.
    """

    def __init__(self, own="mgr", rows=()):
        self.own, self.rows = own, {r.name: r for r in rows}
        if own is not None:
            self.rows.setdefault(own, row(own, view=SessionView(shown="mgr")))
        self.calls, self.starts, self.shows = [], [], []
        self.error, self.error_after, self.start_error = None, 0, None
        self.events = [IdleTimeout(workers.EARLY)]
        self.dead = self.placed = self.on_show = self.handle = None
        self.gone, self.tuis, self.stuck = False, {}, set()

    def sessions(self, match=None):
        self.calls.append(("sessions", match))
        if self.error is not None and len(self.ran("sessions")) > self.error_after:
            raise self.error
        return [
            dataclasses.replace(r, own=n == self.own)
            for n, r in sorted(self.rows.items())
            if match is None or re.fullmatch(match, n)
        ]

    def start(self, runtime, mode, prompt, spec, *, options=None):
        options = StartOptions() if options is None else options
        resume = options.resume
        kw = {
            "name": spec.name,
            "sid": spec.sid,
            "idle_timeout": options.idle_timeout,
            "events": options.events,
        }
        self.calls.append(("start", kw["name"]))
        self.starts.append(
            dict(runtime=runtime, mode=mode, prompt=prompt, spec=spec, resume=resume, **kw)
        )
        if self.start_error is not None:
            raise self.start_error
        if not self.gone:
            self.rows[kw["name"]] = row(
                kw["name"],
                resume or kw["sid"],
                view=SessionView(
                    state="dead" if self.dead else "working", runtime=runtime, last_lines=self.dead
                ),
            )
        self.handle = Handle(self.events)
        return self.handle

    def show(self, name, split=None, split_from=None, opener=None):
        self.calls.append(("show", name))
        self.shows.append((name, split, split_from))
        if self.on_show:
            self.on_show(name)
        if self.placed and name in self.rows:
            pane, opener = self.placed
            self.rows[name] = dataclasses.replace(self.rows[name], pane=pane, opener=opener)

    def attach_command(self, name):
        return f"tmux attach -t '={name}'"

    def close(self, name):
        self.calls.append(("close", name))
        if name in self.stuck:
            raise FakeDriverError.close_failed(name)
        self.rows.pop(name, None)

    def attach(self, name):
        self.calls.append(("attach", name))
        if name not in self.tuis:
            raise FakeDriverError.no_tui(name)
        return Attached(self, name)

    def ran(self, what):
        return [c[1] if len(c) == 2 else c[1:] for c in self.calls if c[0] == what]


class Runs:
    """results by script, or by worker name for the crew module."""

    def __init__(self, results=None, lock=None):
        self.results, self.lock, self.recoveries = results or {}, lock, []

    @staticmethod
    def key(argv):
        return argv[5] if argv[1:4] == MODULE else argv[1]

    def __call__(self, argv, **kw):
        self.recoveries.append((argv, kw, self.lock is None or lock_free(self.lock)))
        result = self.results.get(self.key(argv), (0, ""))
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(argv, result[0], result[1])
