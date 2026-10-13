"""Synchronization behavior."""

import dataclasses
import json
import os
import stat

from agent_pm.manager.engine import monitor

from manager.tests.unit.crew.case import AttachCase
from manager.tests.unit.crew.data import (
    HEADER,
    MGR,
    MODULE,
    OTHER,
    PY,
    SCRIPT,
    SID,
    SID2,
    WHEN,
    SessionView,
    entry,
    fixed_time,
    recovered,
    result,
    row,
    tail,
    within,
)


class SynchronizationTest(AttachCase):
    def test_first_attach_makes_the_directory_and_files_and_takes_the_lease(self):
        with fixed_time(WHEN):
            self.assertEqual(self.synced(self.server()), HEADER + tail(self.mdir))
        self.assertEqual(stat.S_IMODE(os.stat(self.mdir).st_mode), 0o700)
        for name in ("events", "roster.json", "roster.lock"):
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.join(self.mdir, name)).st_mode), 0o600, name
            )
        self.assertEqual(
            json.loads(self.bytes()),
            {"version": 1, "holder": MGR, "cursor": 0, "gen": 0, "entries": {}},
        )
        with fixed_time("2026-10-10T00:00:00+00:00"):
            self.synced(self.server())
        self.assertEqual(json.loads(self.bytes())["holder"], MGR)

    def test_manager_option_names_the_directory_the_own_session_holds(self):
        self.synced(self.server("m2"), "--manager", "mgr")
        self.assertEqual(json.loads(self.bytes())["holder"]["session"], "m2")

    def test_refused_by_a_live_other_holder_writing_nothing(self):
        for own in ("mgr", None):
            with self.subTest(own=own):
                before = self.seed(holder=OTHER, w1=entry())
                fake = self.server(own, [row("other")])
                self.assertEqual(self.attach(fake, "--manager", "mgr"), (1, "", self.held()))
                self.assertEqual(self.bytes(), before)
                self.assertEqual(fake.ran("sessions"), [None, "other"])

    def test_outside_tmux_prints_the_notice_and_leaves_the_holder(self):
        notice = f"workers: not in tmux: no lease on {self.mdir}; another manager may attach\n"
        for holder in (None, {"session": "old", "since": WHEN}):
            with self.subTest(holder=holder):
                self.seed(holder=holder)
                self.assertEqual(
                    self.attach(self.server(None), "--manager", "mgr"),
                    (0, HEADER + tail(self.mdir), notice),
                )
                self.assertEqual(json.loads(self.bytes())["holder"], holder)

    def test_outside_tmux_without_manager_exits_1_and_makes_nothing(self):
        self.assertEqual(
            self.attach(self.server(None)),
            (1, "", "workers: no manager directory: run inside tmux or give --manager <name>\n"),
        )
        self.assertEqual(self.made(), [])

    def test_a_worker_renamed_in_tmux_is_rekeyed_by_its_sid(self):
        old = entry(resume=[PY, SCRIPT["worker"], "start", "w1"], state="gone")
        self.seed(w1=old)
        self.synced(
            self.server(
                rows=[
                    row(
                        "w9",
                        SID,
                        view=SessionView(state="done", pane="%4", opener="mgr", shown="mgr"),
                    )
                ]
            )
        )
        self.assertEqual(
            self.entries(), {"w9": {**old, "state": "done", "pane": "%4", "opener": "mgr"}}
        )

    def test_no_rename_without_exactly_one_match_free_to_take(self):
        cases = {
            "two sessions have its sid": ([row("w8", SID), row("w9", SID)], {}),
            "the match is an entry": ([row("w9", SID)], {"w9": entry("role", sid=SID2)}),
            "no session has its sid": ([row("w9", SID2)], {}),
        }
        for why, (rows, others) in cases.items():
            with self.subTest(why):
                self.seed(w1=entry(), **others)
                rows = [dataclasses.replace(r, shown="mgr") for r in rows]
                self.synced(self.server(rows=rows))
                self.assertEqual(set(self.entries()), {"w1", *others})
                self.assertEqual(self.entries()["w1"]["state"], "gone")

    def test_only_a_worker_with_a_sid_is_renamed(self):
        self.seed(
            r1=entry("role", cwd=self.dir),
            p1=entry("pipeline", sid=SID2, cwd=self.dir),
            w1=entry(sid=None),
        )
        rows = [
            row("x1", SID, view=SessionView(shown="mgr")),
            row("x2", SID2, view=SessionView(shown="mgr")),
            row("x3", view=SessionView(shown="mgr")),
        ]
        self.synced(self.server(rows=rows))
        self.assertEqual(
            {n: e["state"] for n, e in self.entries().items()},
            {"r1": "gone", "p1": "gone", "w1": "gone"},
        )

    def test_a_live_workers_state_is_its_rows(self):
        states = ("working", "done", "blocked", "dead")
        self.seed(**{f"w{i}": entry(state="gone") for i in range(len(states))})
        rows = [
            row(f"w{i}", SID, view=SessionView(state=state, shown="mgr"))
            for i, state in enumerate(states)
        ]
        self.synced(self.server(rows=rows))
        self.assertEqual(
            [self.entries()[f"w{i}"]["state"] for i in range(len(states))], list(states)
        )

    def test_a_worker_without_a_session_is_gone(self):
        self.seed(w1=entry(state="blocked", pane="%4"))
        self.assertEqual(
            self.synced(self.server()),
            HEADER + f"w1\tworker\t{SID}\tgone\t-\t/w\t%4\n"
            f"resume w1: {recovered([PY, *MODULE, '--go'])}\n" + tail(self.mdir),
        )
        self.assertEqual(self.entries()["w1"]["state"], "gone")
        self.assertEqual(self.shows, [])

    def test_a_worker_without_a_session_is_gone_whatever_run_record_its_cwd_holds(self):
        entries = {}
        for status in ("needs_input", "done", "failed"):
            cwd = os.path.join(self.dir, status)
            os.mkdir(cwd)
            with open(os.path.join(cwd, "run.jsonl"), "w") as f:
                f.write(result(outcome={"status": status}))
            entries[status] = entry(cwd=cwd)
        self.seed(**entries)
        out = self.synced(self.server())
        self.assertEqual(
            {n: e["state"] for n, e in self.entries().items()}, dict.fromkeys(entries, "gone")
        )
        self.assertEqual(
            [line.split(":")[0] for line in out.splitlines() if line.startswith("resume ")],
            [f"resume {name}" for name in sorted(entries)],
        )

    def test_a_role_or_pipeline_is_live_by_its_driver_or_tui_session(self):
        for kind in ("role", "pipeline"):
            with self.subTest(kind=kind):
                self.shows = []
                self.seed(
                    d1=entry(kind, cwd=self.dir, state="gone", tui="t1", pane="%1"),
                    d2=entry(kind, cwd=self.dir, state="done"),
                    t3=entry(kind, cwd=self.dir, state="gone", tui="t3t"),
                    b4=entry(kind, cwd=self.dir, state="gone", tui="t4"),
                )
                rows = [
                    row("d1", view=SessionView(state="dead", pane="%7", opener="x")),
                    row("d2", view=SessionView(state="blocked")),
                    row(
                        "t3t",
                        SID,
                        view=SessionView(state="blocked", pane="%8", opener="mgr", shown="mgr"),
                    ),
                    row("b4", view=SessionView(state="dead")),
                    row("t4", view=SessionView(state="done", pane="%6", shown="mgr")),
                ]
                self.synced(self.server(rows=rows))
                self.assertEqual(
                    {n: (e["state"], e["pane"], e["opener"]) for n, e in self.entries().items()},
                    {
                        "d1": ("working", "%1", None),
                        "d2": ("working", None, None),
                        "t3": ("blocked", "%8", "mgr"),
                        "b4": ("done", "%6", None),
                    },
                )
                self.assertEqual(self.shows, [])

    def test_a_role_or_pipeline_without_a_session_is_finished_waiting_or_gone_by_its_run_record(
        self,
    ):
        done, failed, run_tail = (
            result(outcome={"status": "done"}),
            result(outcome={"status": "failed"}),
            monitor.RUN_TAIL,
        )
        asking = result(outcome={"status": "needs_input"})
        pad = ("x" * 99 + "\n") * (run_tail // 100 + 1)
        cut = run_tail - len(done)
        records = {
            "done": (done, "finished"),
            "failed": (failed, "finished"),
            "later-lines-skipped": (
                done + '{"kind": "end"}\nnot json\n' + "[" * 200000 + '\n{"kind": "res',
                "finished",
            ),
            "last-result-wins": (done + result(error="stopped: KeyboardInterrupt"), "gone"),
            "failed-after-an-error": (result(error="boom") + failed, "finished"),
            "needs-input": (asking, "waiting"),
            "answered": (asking + done, "finished"),
            "asking-after-done": (done + asking, "waiting"),
            "needs-input-after-an-error": (result(error="boom") + asking, "waiting"),
            "error-after-needs-input": (
                asking + result(error="stopped: KeyboardInterrupt"),
                "gone",
            ),
            "error": (result(error="boom"), "gone"),
            "malformed": ("{\nnot json\n\udcff\n", "gone"),
            "empty": ("", "gone"),
            "outcome-a-string": (result(outcome="done"), "gone"),
            "outcome-a-list": (result(outcome=[{"status": "done"}]), "gone"),
            "status-a-list": (result(outcome={"status": ["done"]}), "gone"),
            "status-an-object": (result(outcome={"status": {"needs_input": 1}}), "gone"),
            "status-other": (result(outcome={"status": "waiting"}), "gone"),
            "no-status": (result(outcome={}), "gone"),
            "kind-outcome": (
                json.dumps({"kind": "outcome", "outcome": {"status": "done"}}) + "\n",
                "gone",
            ),
            "a-list-line": (
                json.dumps([{"kind": "result", "outcome": {"status": "done"}}]) + "\n",
                "gone",
            ),
            "beyond-the-tail": (done + pad, "gone"),
            "within-the-tail": (pad + done, "finished"),
            "cut-by-the-tail": ("x" + done + "p\n" * (cut // 2) + "p" * (cut % 2), "gone"),
            "starts-the-tail": ("x\n" + done + "p\n" * (cut // 2) + "p" * (cut % 2), "finished"),
        }
        entries, want = {}, {}
        for i, (name, (text, state)) in enumerate(records.items()):
            cwd = os.path.join(self.dir, name)
            os.mkdir(cwd)
            with open(
                os.path.join(cwd, "run.jsonl"), "w", encoding="utf-8", errors="surrogateescape"
            ) as f:
                f.write(text)
            entries[name], want[name] = entry(("role", "pipeline")[i % 2], cwd=cwd), state
        done_cwd = os.path.join(self.dir, "done", "run.jsonl")
        for name, make in (
            ("missing", lambda _path: None),
            ("symlink", lambda path: os.symlink(done_cwd, path)),
            ("fifo", os.mkfifo),
            ("directory", os.mkdir),
        ):
            cwd = os.path.join(self.dir, name)
            os.mkdir(cwd)
            make(os.path.join(cwd, "run.jsonl"))
            entries[name], want[name] = entry("role", cwd=cwd), "gone"
        self.seed(**entries)
        with within():
            self.synced(self.server())
        self.assertEqual({n: e["state"] for n, e in self.entries().items()}, want)

    def test_a_live_sessions_pane_and_opener_are_synced_when_valid(self):
        cases = {
            "w1": ("%7", "mgr", "%7", "mgr"),
            "w2": ("5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE", "5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE"),
            "w3": (None, None, "%1", "old"),
            "w4": (None, "new", "%1", "new"),
            "w5": ("%8", "a" * 201, "%8", "old"),
        }
        self.seed(**{name: entry(pane="%1", opener="old") for name in cases})
        rows = [
            row(n, SID, view=SessionView(state="working", pane=p, opener=o, shown="mgr"))
            for n, (p, o, _, _) in cases.items()
        ]
        self.synced(self.server(rows=rows))
        self.assertEqual(
            {n: (e["pane"], e["opener"]) for n, e in self.entries().items()},
            {n: (p, o) for n, (_, _, p, o) in cases.items()},
        )
