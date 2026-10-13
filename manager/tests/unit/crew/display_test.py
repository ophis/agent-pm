"""Display behavior."""

import fcntl
import os
import shlex
from unittest import mock

from agent_pm.manager.engine import roster
from agent_pm.session import (
    DriverError,
)

from manager.tests.case import PYTHONPATH
from manager.tests.unit.crew.case import AttachCase
from manager.tests.unit.crew.data import (
    HEADER,
    MGR,
    MODULE,
    PY,
    SID,
    SID2,
    SessionView,
    entry,
    nl,
    recovered,
    result,
    row,
    started,
    tail,
)


class DisplayTest(AttachCase):
    def test_a_live_session_no_client_shows_is_reopened_with_the_lock_free(self):
        self.seed(
            w1=entry(split="below", split_from="mgr"),
            r1=entry("role", tui="r1-tui", split="right"),
            w2=entry(),
            w3=entry(sid=SID2),
            r2=entry("role", sid=SID2, cwd=self.dir, tui="r2-tui"),
        )
        fake = self.server(
            rows=[
                row("w1", SID),
                row("r1-tui", view=SessionView(state="done")),
                row("w2", SID, view=SessionView(shown="mgr")),
                row("r2"),
            ]
        )
        with mock.patch("fcntl.flock", wraps=fcntl.flock) as locked:
            out = self.synced(fake)
        self.assertEqual(self.shows, [("r1-tui", True), ("w1", True)])
        self.assertEqual(fake.shows, [("r1-tui", "right", None), ("w1", "below", "mgr")])
        self.assertEqual(locked.call_count, 4)
        self.assertEqual(
            {n: (e["pane"], e["opener"]) for n, e in self.entries().items()},
            {
                "w1": ("%9", "mgr"),
                "r1": ("%9", "mgr"),
                "w2": (None, None),
                "w3": (None, None),
                "r2": (None, None),
            },
        )
        self.assertIn(f"w1\tworker\t{SID}\tworking\t-\t/w\t%9\n", out)

    def test_no_reopen_no_second_roster_block(self):
        self.seed(w1=entry())
        with mock.patch("fcntl.flock", wraps=fcntl.flock) as locked:
            self.synced(self.server(rows=[row("w1", SID, view=SessionView(shown="mgr"))]))
        self.assertEqual((locked.call_count, self.shows), (1, []))

    def test_placement_is_recorded_only_for_an_entry_still_the_same(self):
        def drop():
            with roster.locked(self.mdir) as r:
                roster.remove(r, "w1")

        cases = {
            "same": (None, {**entry(), "pane": "%9", "opener": "mgr"}),
            "sid changed": (lambda: roster.put(self.mdir, "w1", entry(sid=SID2)), entry(sid=SID2)),
            "kind changed": (
                lambda: roster.put(self.mdir, "w1", entry("pipeline")),
                entry("pipeline"),
            ),
            "removed": (drop, None),
        }
        for why, (then, want) in cases.items():
            with self.subTest(why):
                self.seed(w1=entry())
                self.then = then
                self.synced(self.server(rows=[row("w1", SID)]))
                self.assertEqual(self.entries().get("w1"), want)

    def test_a_shown_session_is_marked_by_the_session_showing_it(self):
        cases = {"w1": ("mgr", "mgr"), "w2": ("other", "other"), "w3": ("", "a terminal")}
        self.seed(**{name: entry() for name in cases})
        rows = [row(n, SID, view=SessionView(shown=shown)) for n, (shown, _) in cases.items()]
        self.assertEqual(
            self.synced(self.server(rows=rows)),
            HEADER
            + "".join(
                f"{n}\tworker\t{SID}\tworking\t-\t/w\t-\tshown in {where}\n"
                for n, (_, where) in cases.items()
            )
            + tail(self.mdir),
        )
        self.assertEqual(self.shows, [])

    def test_table_and_recovery_commands_none_for_a_waiting_run(self):
        cwd = os.path.join(self.dir, "it's a role")
        finished, asking = os.path.join(self.dir, "finished"), os.path.join(self.dir, "asking")
        for d, status in ((cwd, None), (finished, "done"), (asking, "needs_input")):
            os.mkdir(d)
            if status:
                with open(os.path.join(d, "run.jsonl"), "w") as f:
                    f.write(result(outcome={"status": status}))
        self.seed(
            b=entry(note="check the logs", pane="%4"),
            a=entry(sid=None, resume=[PY, "/x/crew.py", "; rm -rf ~"]),
            B=entry("role", cwd=cwd, resume=[PY, "/x/drive.py", "--role", "pm"]),
            C=entry("role", sid=None, cwd=cwd),
            p1=entry("pipeline", cwd=cwd, resume=[PY, "/x/router.py", "--plan", "/p q"]),
            f1=entry("pipeline", cwd=finished),
            n1=entry("role", cwd=asking),
            n2=entry("pipeline", cwd=asking),
        )
        out = self.synced(
            self.server(
                rows=[row("b", SID, view=SessionView(state="working", pane="%4", shown="mgr"))]
            )
        )
        role = (
            f"cd {shlex.quote(cwd)} && {PY} /x/drive.py --role pm --sid {SID} --resume "
            "--input 'Continue the unfinished task.'"
        )
        self.assertEqual(
            out,
            HEADER
            + nl(
                f"B\trole\t{SID}\tgone\t-\t{cwd}\t-",
                f"C\trole\t-\tgone\t-\t{cwd}\t-",
                "a\tworker\t-\tgone\t-\t/w\t-",
                f"b\tworker\t{SID}\tworking\tcheck the logs\t/w\t%4\tshown in mgr",
                f"f1\tpipeline\t{SID}\tfinished\t-\t{finished}\t-",
                f"n1\trole\t{SID}\twaiting\t-\t{asking}\t-",
                f"n2\tpipeline\t{SID}\twaiting\t-\t{asking}\t-",
                f"p1\tpipeline\t{SID}\tgone\t-\t{cwd}\t-",
                f"resume B: {role}",
                "resume C: none (no sid)",
                f"resume a: {recovered([PY, *MODULE, '; rm -rf ~'])}",
                f"resume p1: {PY} /x/router.py --plan '/p q'",
            )
            + tail(self.mdir),
        )
        self.assertEqual(shlex.split(role)[:4], ["cd", cwd, "&&", PY])
        self.assertEqual(
            shlex.split(out.splitlines()[-6].split(": ", 1)[1]),
            ["env", f"PYTHONPATH={PYTHONPATH}", PY, *MODULE, "; rm -rf ~"],
        )

    def test_a_worker_entries_resume_line_names_the_import_root_then_the_callers_pythonpath(self):
        e = entry(resume=started("w1", "--client", "rt0"))
        self.seed(w1=e)
        with mock.patch.dict(os.environ, {"PYTHONPATH": f"{PYTHONPATH}:/u 1"}):
            out = self.synced(self.server())
        (line,) = (x for x in out.splitlines() if x.startswith("resume w1: "))
        self.assertEqual(
            shlex.split(line.removeprefix("resume w1: ")),
            ["env", f"PYTHONPATH={PYTHONPATH}:/u 1", *roster.recovery(e)],
        )

    def test_a_worker_entry_without_a_client_recovers_with_the_configs_runtime(self):
        resume = [PY, "/x/crew.py", "start", "w1", "--manager", "mgr"]
        self.seed(w1=entry(resume=resume))
        out = self.synced(self.server())
        self.assertIn(
            f"resume w1: {recovered(started('w1', '--client', 'rt0', '--manager', 'mgr'))}\n", out
        )
        self.local('runtime = ""\n')
        self.assertEqual(
            self.attach(self.server()),
            (1, "", "workers: config: runtime: want a runtime name\n"),
        )

    def test_an_invalid_entry_is_skipped_with_one_stderr_line(self):
        self.seed(
            w1=entry(), bad=entry(sid="; rm -rf ~"), rel=entry(resume=["python3", "/x/crew.py"])
        )
        rc, out, err = self.attach(self.server(rows=[row("w1", SID, view=SessionView(shown=""))]))
        self.assertEqual(
            (rc, out),
            (
                0,
                HEADER
                + f"w1\tworker\t{SID}\tworking\t-\t/w\t-\tshown in a terminal\n"
                + tail(self.mdir),
            ),
        )
        self.assertEqual(
            err,
            "manager: roster.json: entry 'bad': sid: '; rm -rf ~': want a session id or null\n"
            "manager: roster.json: entry 'rel': resume[0]: 'python3': want an absolute path\n",
        )
        self.assertEqual(set(self.entries()), {"w1"})

    def test_no_tmux_server_means_no_live_session(self):
        self.seed(w1=entry())
        rc, out, _ = self.attach(self.server(None), "--manager", "mgr")
        self.assertEqual((rc, out.splitlines()[1]), (0, f"w1\tworker\t{SID}\tgone\t-\t/w\t-"))
        self.assertEqual(self.entries()["w1"]["state"], "gone")

    def test_unknown_sessions_write_nothing(self):
        msg = "workers: tmux sessions unknown: tmux failed\n"
        for after in (0, 1):
            with self.subTest(after=after):
                before = self.seed(MGR, w1=entry())
                fake = self.server()
                fake.error, fake.error_after = DriverError(msg[9:-1]), after
                self.assertEqual(self.attach(fake, "--manager", "mgr"), (1, "", msg))
                self.assertEqual(self.bytes(), before)
        os.unlink(self.path)
        fake = self.server()
        fake.error, fake.error_after = DriverError("x"), 1
        self.assertEqual(self.attach(fake, "--manager", "mgr")[0], 1)
        self.assertFalse(os.path.exists(self.path))

    def test_a_reopen_prints_the_attach_line_on_stderr(self):
        self.seed(w1=entry())
        rc, out, err = self.attach(self.server(None, [row("w1", SID)]), "--manager", "mgr")
        self.assertEqual(
            (rc, out), (0, HEADER + f"w1\tworker\t{SID}\tworking\t-\t/w\t%9\n" + tail(self.mdir))
        )
        self.assertEqual(
            err,
            f"workers: not in tmux: no lease on {self.mdir}; another manager may attach\n"
            "tui: session w1: tmux attach -t '=w1'\n",
        )
