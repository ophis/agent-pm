"""Lease behavior."""

import datetime
import functools
import json
import os
import re
import subprocess

from agent_pm.manager.engine import roster
from agent_pm.session import DriverError

from manager.tests.case import PY, child_env
from manager.tests.unit.roster.case import Base
from manager.tests.unit.roster.data import ATTACH, KEPT, LEASE_CHILD, NOW, SID2, WHEN, good, kills


class LeaseTest(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "roster.json")
        roster.ensure(self.dir)

    def doc(self, holder=None):
        return {"version": 1, "holder": holder, "cursor": 3, "gen": 1, "entries": {}}

    def held(self, session="other", since=WHEN):
        return {"session": session, "since": since}

    def message(self, session="other", since=WHEN):
        return (
            f"manager directory {self.dir}: held by tmux session {session} since {since}; "
            "ask that manager to run crew.py release, or end that session"
        )

    def refused(self, call, r, message, kind=roster.RosterError):
        before = json.dumps(r)
        with self.assertRaises(kind) as cm:
            call()
        self.assertIs(type(cm.exception), kind)
        self.assertEqual(str(cm.exception), message)
        self.assertEqual(json.dumps(r), before)

    def test_take_and_check_by_holder_own_and_live_sessions(self):
        since = "2026-01-02T03:04:05+00:00"
        other, me = self.held("other", since), self.held("me")
        held, attach = (
            (roster.Held, self.message("other", since)),
            (roster.RosterError, ATTACH.format(self.dir)),
        )
        cases = [
            ("free", None, "me", (), NOW, attach, []),
            ("own the holder: since kept", me, "me", ("me",), KEPT, KEPT, []),
            ("a live other", other, "me", ("other",), held, held, ["other"]),
            ("a dead other: a new since", other, "me", ("me",), NOW, attach, ["other"]),
            ("unknown sessions count as not live", other, "me", None, NOW, attach, ["other"]),
            ("outside tmux, free", None, None, (), KEPT, KEPT, []),
            ("outside tmux, a dead other", other, None, (), KEPT, KEPT, ["other"]),
            ("outside tmux, a live other", other, None, ("other",), held, held, ["other"]),
            ("outside tmux, unknown sessions", other, None, None, KEPT, KEPT, ["other"]),
        ]
        for label, holder, own, live, take, check, asked in cases:
            for call, want in (("take", take), ("check", check)):
                with self.subTest(label, call=call):
                    self.tmux.alive, self.tmux.calls = set(live or ()), []
                    self.tmux.error = DriverError("tmux sessions unknown") if live is None else None
                    r, start = self.doc(holder), datetime.datetime.now(datetime.UTC)
                    run = (
                        functools.partial(roster.take, r, self.dir, own, driver=self.driver)
                        if call == "take"
                        else functools.partial(roster.check, r, self.dir, own, driver=self.driver)
                    )
                    if isinstance(want, tuple):
                        self.refused(run, r, want[1], want[0])
                    elif want is NOW:
                        run()
                        self.assertEqual(
                            r, self.doc({"session": own, "since": r["holder"]["since"]})
                        )
                        made = datetime.datetime.fromisoformat(r["holder"]["since"])
                        self.assertGreaterEqual(made, start.replace(microsecond=0))
                    else:
                        run()
                        self.assertEqual(r, self.doc(holder if want is KEPT else want))
                    self.assertEqual(self.tmux.calls, [("sessions", re.escape(s)) for s in asked])

    def test_take_under_roster_writes_the_holder_and_a_refusal_writes_nothing(self):
        with roster.locked(self.dir) as r:
            roster.take(r, self.dir, "me", driver=self.driver)
        with open(self.path, "rb") as f:
            raw = f.read()
        holder = json.loads(raw)["holder"]
        self.assertEqual(holder["session"], "me")
        self.assertIsNotNone(datetime.datetime.fromisoformat(holder["since"]).utcoffset())
        self.tmux.alive = {"me"}
        with self.assertRaises(roster.Held), roster.locked(self.dir) as r:
            roster.take(r, self.dir, "you", driver=self.driver)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_check_in_a_read_only_block_creates_no_roster_json(self):
        with roster.locked(self.dir, write=False) as r:
            roster.check(r, self.dir, None, driver=self.driver)
            with self.assertRaises(roster.RosterError):
                roster.check(r, self.dir, "me", driver=self.driver)
        self.assertFalse(os.path.exists(self.path))

    def test_two_processes_take_concurrently_and_exactly_one_holds(self):
        procs = [
            subprocess.Popen(
                [*PY, "-c", LEASE_CHILD, self.dir, own],
                env=child_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for own in ("a", "b")
        ]
        for p in procs:
            p.stdin.write("\n")
            # communicate flushes stdin on 3.11; closing it first raises.
        p.stdin.flush()
        out = {}
        for own, p in zip("ab", procs, strict=False):
            out[own] = p.communicate(timeout=60)
            self.assertIn(p.returncode, (0, 3), out[own][1])
        winners = [own for own, p in zip("ab", procs, strict=False) if p.returncode == 0]
        self.assertEqual(len(winners), 1)
        loser = "b" if winners == ["a"] else "a"
        with open(self.path) as f:
            holder = json.load(f)["holder"]
        self.assertEqual(holder["session"], winners[0])
        self.assertEqual(out[loser][0].strip(), self.message(winners[0], holder["since"]))


class StopTest(Base):
    def setUp(self):
        super().setUp()
        roster.ensure(self.dir)
        self.entries = {
            "w1": good(),
            "w2": good(sid=SID2),
            "d1": good(kind="role", tui="t1"),
            "d2": good(kind="pipeline", tui="t2"),
            "d3": good(kind="role", tui=None),
        }

    def live(self, *live, stuck=(), unknown=False):
        self.tmux.alive, self.tmux.stuck, self.tmux.calls = set(live), set(stuck), []
        self.tmux.error = DriverError("tmux sessions unknown") if unknown else None

    def stop(self, name):
        with roster.locked(self.dir) as r:
            r["entries"] = dict(self.entries)
        with roster.locked(self.dir) as r:
            key = roster.stop(r, name, driver=self.driver)
        with roster.locked(self.dir, write=False) as r:
            return key, r["entries"]

    def refused(self, name, message):
        with roster.locked(self.dir) as r:
            r["entries"] = dict(self.entries)
        with self.assertRaises(roster.RosterError) as cm, roster.locked(self.dir) as r:
            roster.stop(r, name, driver=self.driver)
        self.assertEqual(str(cm.exception), message)
        with roster.locked(self.dir, write=False) as r:
            self.assertEqual(r["entries"], self.entries)

    def test_closes_the_sessions_driver_first_then_removes_the_entry(self):
        cases = [
            ("a worker", "w1", ("w1", "w2"), "w1", ("w1",)),
            ("a role by its key", "d1", ("d1", "t1"), "d1", ("d1", "t1")),
            ("a role by its tui", "t1", ("d1", "t1"), "d1", ("d1", "t1")),
            ("a pipeline by its key", "d2", ("d2", "t2"), "d2", ("d2", "t2")),
            ("a pipeline by its tui", "t2", ("d2", "t2"), "d2", ("d2", "t2")),
            ("a worker gone already", "w1", (), "w1", ("w1",)),
            ("a role's driver gone already", "d1", ("t1",), "d1", ("d1", "t1")),
            ("a role without a tui, gone already", "d3", (), "d3", ("d3",)),
            ("unknown sessions count as gone", "w1", None, "w1", ("w1",)),
        ]
        for label, name, live, key, closed in cases:
            with self.subTest(label):
                self.live(*(live or ()), unknown=live is None)
                self.assertEqual(
                    self.stop(name),
                    (key, {n: e for n, e in self.entries.items() if n != key}),
                )
                self.assertEqual(self.tmux.calls, kills(*closed))

    def test_an_entry_key_beats_another_entries_tui(self):
        self.entries["t1"] = good()
        self.live("t1", "d1")
        self.assertEqual(self.stop("t1")[0], "t1")
        self.assertEqual(self.tmux.calls, kills("t1"))

    def test_not_in_roster_or_a_session_still_live_keeps_every_entry(self):
        cases = [
            ("not in roster: nothing closed", "w9", ("w9",), (), "w9 not in roster", ()),
            (
                "a close failing, its session still live",
                "d1",
                ("d1", "t1"),
                ("t1",),
                "d1: session t1 still live after tmux kill-session",
                ("d1", "t1"),
            ),
        ]
        for label, name, live, stuck, message, closed in cases:
            with self.subTest(label):
                self.live(*live, stuck=stuck)
                self.refused(name, message)
                self.assertEqual(self.tmux.calls, kills(*closed))
