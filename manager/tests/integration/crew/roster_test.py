"""Roster behavior."""

import json
import os
import shlex
import stat
import subprocess

from agent_pm.manager.engine import monitor, roster

from manager.tests.case import PYTHONPATH
from manager.tests.integration.crew.case import Live, RosterLive
from manager.tests.integration.crew.data import (
    CREW,
    MANAGER,
    ROLE_DRIVER,
    ROLE_SID,
    ROLE_TUI,
    SECOND,
    TIMEOUT,
    WIDTH,
    filler,
    has_pair,
    is_event,
    logged,
    raw,
    tail,
)
from session.tests import live_tmux
from session.tests.integration.adapters.claude import fake_claude


class ManagersTest(Live):
    ATTACH = False

    def test_own_session_and_worker_in_worker(self):
        self.attach()
        sid = self.started("w1")
        self.assertEqual(self.record(sid)["events"], self.events)
        self.wait_event("w1", "done")
        self.attach(inside="w1")
        sid = self.started("w2", inside="w1")
        inner = self.events_of("w1")
        self.assertEqual(self.record(sid)["events"], inner)
        self.assertEqual(
            [x.split(" ")[1:] for x in self.wait_event("w2", "done", events=inner)],
            [["w2", "done"]],
        )
        self.assertEqual(
            [x.split(" ")[1:] for x in live_tmux.events(self.events)], [["w1", "done"]]
        )

    def test_manager_flag(self):
        self.attach("--manager", "other")
        sid = self.started("w1", "--manager", "other")
        other = self.events_of("other")
        self.assertEqual(self.record(sid)["events"], other)
        (done,) = self.wait_event("w1", "done", events=other)
        self.assertEqual(self.next_event(0, "--manager", "other"), (1, done, "Hi from w1."))
        self.assertFalse(os.path.exists(self.directory))

    def test_no_pane_needs_a_manager(self):
        res = self.start("w1", inside=None)
        self.assertEqual(
            (res.returncode, res.stderr),
            (1, "workers: no manager directory: run inside tmux or give --manager <name>\n"),
        )
        self.assertFalse(os.path.exists(os.path.dirname(self.directory)))
        self.assertTrue(self.gone("w1"))


class RosterTest(RosterLive):
    def test_second_manager_after_release(self):
        self.attach()
        self.second()
        self.refused()
        self.assertEqual(
            self.ok(self.workers("release")).stdout, f"workers: released {self.directory}\n"
        )
        self.assertIsNone(self.roster()["holder"])
        self.taken_over()

    def test_second_manager_after_the_holder_ends(self):
        self.attach()
        self.second()
        self.refused()
        self.kill(MANAGER)
        self.taken_over()

    def test_concurrent_attaches_one_holds(self):
        self.second()
        procs = {
            s: subprocess.Popen(
                [*CREW, "attach", "--manager", MANAGER],
                env=self.server.env(PYTHONPATH=PYTHONPATH, **self.server.inside(s)),
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for s in (MANAGER, SECOND)
        }
        for p in procs.values():
            self.addCleanup(p.communicate)  # cleanups run last first: after the kill, reaps it
            self.addCleanup(p.kill)
        out = {s: p.communicate(timeout=TIMEOUT) for s, p in procs.items()}
        winners = [s for s, p in procs.items() if p.returncode == 0]
        self.assertEqual(len(winners), 1, out)
        (winner,) = winners
        (loser,) = set(procs) - {winner}
        self.assertEqual(out[winner], (f"{monitor.HEADER}\n{tail(self.directory)}", ""))
        self.assertEqual((procs[loser].returncode, out[loser]), (1, ("", self.held(winner))))

    def test_a_printed_resume_line_gives_the_recovered_worker_the_callers_pythonpath(self):
        user = os.path.join(self.root, "user lib")
        given = f"{PYTHONPATH}{os.pathsep}{user}"
        claude = self.dumping("claude", fake_claude.__file__)
        self.attach(PYTHONPATH=given)
        self.started("w1", PYTHONPATH=given)
        self.wait_event("w1", "done")
        self.kill("w1")
        out = self.attach(PYTHONPATH=given).stdout
        (line,) = (x for x in out.splitlines() if x.startswith("resume w1: "))
        self.ok(self.call("sh", "-c", line.removeprefix("resume w1: "), **self.scene("w1")))
        live_tmux.wait(lambda: len(logged(claude)) > 1, what="the recovered worker's claude run")
        self.assertEqual([e["env"].get("PYTHONPATH") for e in logged(claude)], [user, user])

    def test_stop_all_stops_what_mgr_opened_and_only_that(self):
        self.attach()
        self.started("w1")
        self.started("w2")
        self.attach(inside="w1")
        self.started("w3", inside="w1")
        self.role(self.cwd, ROLE_DRIVER, ROLE_TUI)
        self.second()
        self.ok(self.server.tmux("new-session", "-d", "-s", "o1", *live_tmux.IDLE))
        self.ok(self.server.tmux("set-option", "-t", "=o1:", "@opener", SECOND))
        mine = ["w3", ROLE_TUI, ROLE_DRIVER, "w1", "w2"]
        res = self.ok(self.workers("stop-all", "--dry-run"))
        self.assertEqual(res.stdout, "".join(f"would stop {n}\n" for n in mine))
        self.assertTrue(self.live(*mine, "o1"))
        res = self.ok(self.workers("stop-all"))
        self.assertEqual(res.stdout, "".join(f"stopped {n}\n" for n in mine))
        self.assertTrue(self.gone(*mine))
        self.assertTrue(self.live(MANAGER, SECOND, "o1"))
        self.assertEqual(self.roster()["entries"], {})
        self.assertEqual(self.roster(os.path.dirname(self.events_of("w1")))["entries"], {})
        self.assertEqual(
            self.ok(self.workers("stop-all")).stdout, "workers: stop-all: nothing to stop\n"
        )

    def test_stop_a_role_run_by_its_tui_session(self):
        self.attach()
        self.role(self.cwd, ROLE_DRIVER, ROLE_TUI)
        self.assertEqual(list(self.roster()["entries"]), [ROLE_DRIVER])
        self.assertEqual(self.ok(self.workers("stop", ROLE_TUI)).stdout, "")
        self.assertTrue(self.gone(ROLE_DRIVER, ROLE_TUI))
        self.assertEqual(self.roster()["entries"], {})

    def test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume(self):
        self.attach()
        work = os.path.join(self.root, "role-work")
        os.mkdir(work)
        with open(os.path.join(work, "run.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "result", "outcome": {"status": "needs_input"}}) + "\n")
        self.role(work)
        table = f"{monitor.HEADER}\n{ROLE_DRIVER}\trole\t{ROLE_SID}\twaiting\t-\t{work}\t-\n"
        self.assertEqual(self.attach().stdout, table + tail(self.directory))
        self.assertEqual(self.attach("--resume").stdout, table + tail(self.directory))
        self.assertEqual(self.roster()["entries"][ROLE_DRIVER]["state"], "waiting")

    def test_take_over_resumes_from_the_cursor(self):
        self.attach()
        self.started("w1", steps=["One."], turns=[["Two."], ["Three."], ["Four."]])
        (one,) = self.wait_event("w1", "done")
        self.assertEqual(self.next_event(0), (1, one, "One."))
        self.send("w1", "more")
        n, two, content = self.next_event(1)
        self.assertEqual((n, content), (2, "Two."))
        self.send("w1", "more")
        *_, three = self.wait_event("w1", "done", 3)
        resumed = tail(self.directory, 2, 0, f"3 {three}")
        self.after_w1(self.attach("--after", "2", "--gen", "0").stdout, resumed)
        self.kill(MANAGER)
        self.second()
        self.after_w1(self.attach("--manager", MANAGER, inside=SECOND).stdout, resumed)
        self.assertEqual(self.roster()["holder"]["session"], SECOND)
        self.assertEqual(
            self.next_event(2, "--manager", MANAGER, inside=SECOND), (3, three, "Three.")
        )
        waiting = subprocess.Popen(
            [*CREW, "next-event", "--manager", MANAGER, "--after", "3", "--gen", "0"],
            env=self.server.env(PYTHONPATH=PYTHONPATH, **self.server.inside(SECOND)),
            cwd=self.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(waiting.communicate)  # cleanups run last first: after the kill, reaps it
        self.addCleanup(waiting.kill)
        live_tmux.wait(lambda: self.roster()["cursor"] == 3, what="next-event's cursor 3")
        self.send("w1", "more", inside=SECOND)
        out, err = waiting.communicate(timeout=TIMEOUT)
        *head, four = live_tmux.events(self.events)
        self.assertEqual(head, [one, two, three])
        self.assertTrue(is_event(four, "w1", "done"), four)
        self.assertEqual((waiting.returncode, out, err), (0, f"4 {four}\nFour.\n", ""))

    def test_rotation_keeps_the_unhandled_lines(self):
        self.attach()
        self.started("w1", turns=[["Again."]])
        self.wait_event("w1", "done")
        kept = filler("k", 3)
        with open(self.events, "a") as f:
            f.write(
                "".join(f"{line}\n" for line in filler("f", roster.EVENTS_MAX // WIDTH + 1) + kept)
            )
        total = len(live_tmux.events(self.events))
        old, original, ino = f"{self.events}.1", raw(self.events), os.lstat(self.events).st_ino
        backlog = [f"{i} {line}" for i, line in enumerate(kept, 1)]
        res = self.attach("--after", str(total - 3), "--gen", "0")
        self.after_w1(res.stdout, tail(self.directory, 0, 1, *backlog))
        self.assertEqual((os.lstat(old).st_ino, raw(old)), (ino, original))
        self.assertEqual(live_tmux.events(self.events), kept)
        self.assertEqual(stat.S_IMODE(os.lstat(self.events).st_mode), 0o600)
        before = self.roster()
        self.assertEqual((before["cursor"], before["gen"]), (0, 1))
        res = self.workers("next-event", "--after", str(total - 3), "--gen", "0")
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (1, "workers: stale line numbers (gen 0, now 1): run crew.py attach\n", ""),
        )
        self.assertEqual(self.roster(), before)
        self.send("w1", "more")
        n, event, content = self.next_event(3, gen=1)
        self.assertEqual((n, content), (4, "Again."))
        self.assertTrue(is_event(event, "w1", "done"), event)
        self.assertEqual(live_tmux.events(self.events), [*kept, event])
        self.assertEqual(raw(old), original)


class RosterRecoveryTest(RosterLive):
    def test_killed_worker_gone_then_recovered(self):
        self.attach()
        sid = self.started("w1")
        pane = self.server.pane_of("w1")
        resume = self.worker_entry("w1", sid)
        e = self.roster()["entries"]["w1"]
        self.assertEqual(
            {k: e[k] for k in ("kind", "sid", "cwd", "resume", "pane", "opener", "state")},
            {
                "kind": "worker",
                "sid": sid,
                "cwd": self.cwd,
                "resume": resume,
                "pane": pane,
                "opener": MANAGER,
                "state": "working",
            },
        )
        (done,) = self.wait_event("w1", "done")  # its transcript, which --resume needs
        self.kill("w1")
        self.assertEqual(
            self.attach().stdout,
            f"{monitor.HEADER}\nw1\tworker\t{sid}\tgone\t-\t{self.cwd}\t{pane}\n"
            f"resume w1: env PYTHONPATH={shlex.quote(PYTHONPATH)} {shlex.join(resume)}\n"
            f"{tail(self.directory, 0, 0, f'1 {done}')}",
        )
        self.assertEqual(self.roster()["entries"]["w1"]["state"], "gone")
        res = self.ok(self.recover("w1", **self.scene("w1")))
        self.assertEqual(res.stdout, f"w1 {sid}\n")
        self.assertTrue(has_pair(self.calls("w1")[-1]["argv"], "--resume", sid))
        self.grid([["w1"]])
        pane = self.server.pane_of("w1")
        table = (
            f"{monitor.HEADER}\nw1\tworker\t{sid}\tworking\t-\t{self.cwd}\t{pane}"
            f"\tshown in {MANAGER}\n"
        )
        out = self.attach().stdout
        self.assertEqual(out[: len(table)], table, out)
        self.assertEqual(list(self.roster()["entries"]), ["w1"])

    def test_attach_resume_brings_back_a_killed_worker_not_a_stopped_one(self):
        self.attach()
        sids = {name: self.started(name) for name in ("w1", "w2")}
        for name in sids:
            self.wait_event(name, "done")  # its transcript, which --resume needs
        self.kill("w1")
        self.assertEqual(self.ok(self.workers("stop", "w2")).stdout, "")
        self.assertTrue(self.gone("w1", "w2"))
        self.assertEqual(list(self.roster()["entries"]), ["w1"])
        out = self.attach().stdout
        self.assertIn("\nresume w1: ", out)
        self.assertNotIn("resumed", out)
        self.assertTrue(self.gone("w1"))
        out = self.attach("--resume", **self.scene("w1")).stdout
        self.assertRegex(out, rf"\nnext-event: [^\n]+\nresumed w1: exit 0: w1 {sids['w1']}\n\Z")
        self.assertTrue(self.live("w1") and self.gone("w2"))
        self.assertTrue(has_pair(self.calls("w1")[-1]["argv"], "--resume", sids["w1"]))
        self.grid([["w1"]])
        self.attach()
        self.assertEqual(
            {n: e["state"] for n, e in self.roster()["entries"].items()}, {"w1": "working"}
        )

    def test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker(self):
        self.attach()
        sids = {name: self.started(name) for name in ("w1", "w2")}
        for name in sids:
            self.wait_event(name, "done")
        self.ok(self.workers("stop", "w2"))
        self.server.kill()
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        out = self.attach("--resume", **self.scene("w1")).stdout
        self.assertRegex(out, rf"\nresumed w1: exit 0: w1 {sids['w1']}\n\Z")
        self.assertTrue(self.live("w1") and self.gone("w2"))
        self.assertTrue(has_pair(self.calls("w1")[-1]["argv"], "--resume", sids["w1"]))
        self.attach()
        self.assertEqual(
            {n: e["state"] for n, e in self.roster()["entries"].items()}, {"w1": "working"}
        )

    def test_unshown_worker_reopened(self):
        self.attach()
        sid = self.started("w1")
        (done,) = self.wait_event("w1", "done")
        self.grid([["w1"]])
        closed = self.server.pane_of("w1")
        self.ok(self.server.tmux("kill-pane", "-t", closed))
        live_tmux.wait(
            lambda: not self.ok(self.server.tmux("list-clients", "-t", "=w1")).stdout,
            what="w1 shown by no client",
        )
        res = self.attach()
        pane = self.server.pane_of("w1")
        self.assertNotEqual(pane, closed)
        self.grid([["w1"]])
        self.assertEqual(self.server.option("w1", "@opener"), MANAGER)
        self.assertEqual(
            res.stdout,
            f"{monitor.HEADER}\nw1\tworker\t{sid}\tdone\t-\t{self.cwd}\t{pane}\n"
            f"{tail(self.directory, 0, 0, f'1 {done}')}",
        )
        self.assertEqual(res.stderr, "tui: session w1: tmux attach -t '=w1'\n")
        e = self.roster()["entries"]["w1"]
        self.assertEqual((e["pane"], e["opener"]), (pane, MANAGER))
