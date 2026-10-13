"""Entries behavior."""

import contextlib
import datetime
import io
import os

from agent_pm.manager.engine import roster
from agent_pm.manager.engine.types import Entry

from manager.tests.case import SRC
from manager.tests.unit.roster.case import RosterCase
from manager.tests.unit.roster.data import KIND, MODULE, NO_RUNTIME, SID, SID2, WHEN, good


class EntriesTest(RosterCase):
    def test_entry_defaults_and_validity(self):
        e = roster.entry(Entry("worker", sid=SID, cwd="/w", resume=good()["resume"]))
        self.assertEqual(sorted(e), sorted(good()))
        self.assertEqual({**e, "started": WHEN}, good())
        self.assertEqual(e["state"], "working")
        self.assertIsNotNone(datetime.datetime.fromisoformat(e["started"]).tzinfo)
        full = roster.entry(
            Entry(
                "role",
                sid=None,
                cwd="/w",
                resume=["/p", "/x/drive.py"],
                note="n",
                opener="mgr",
                pane="%3",
                split="right",
                split_from="a",
                tui="t",
                state="done",
                started=WHEN,
            )
        )
        self.assertEqual(
            full,
            good(
                kind="role",
                sid=None,
                resume=["/p", "/x/drive.py"],
                note="n",
                opener="mgr",
                pane="%3",
                split="right",
                split_from="a",
                tui="t",
                state="done",
            ),
        )
        with self.assertRaises(roster.RosterError) as cm:
            roster.entry(Entry("boss", sid=SID, cwd="/w", resume=["/p", "/x/crew.py"]))
        self.assertEqual(str(cm.exception), "entry: " + KIND.format("boss"))
        with self.assertRaisesRegex(roster.RosterError, "^entry: cwd: 'rel': want"):
            roster.entry(Entry("worker", sid=SID, cwd="rel", resume=["/p", "/x/crew.py"]))

    def test_set_entry_and_record_validate_replace_and_leave_the_dict_on_a_fault(self):
        for setter in (roster.set_entry, roster.record):
            with self.subTest(setter.__name__):
                with roster.locked(self.dir) as r:
                    r["entries"] = {}
                    setter(r, "w1", good())
                    setter(r, "w1", good(note="n"))
                    with self.assertRaises(roster.RosterError) as cm:
                        setter(r, "w2", good(sid="x"))
                    self.assertEqual(
                        str(cm.exception), "entry 'w2': sid: 'x': want a session id or null"
                    )
                    with self.assertRaisesRegex(roster.RosterError, "^entry 'w1': sid: 'x': want"):
                        setter(r, "w1", good(sid="x"))
                    with self.assertRaisesRegex(roster.RosterError, "^entry 'a b': name: want"):
                        setter(r, "a b", good())
                self.assertEqual(self.entries(), {"w1": good(note="n")})

    def test_record_keeps_the_note_of_a_replaced_entry_of_the_same_sid(self):
        cases = {
            "same sid, note None": (
                good(note="n"),
                good(state="done"),
                good(state="done", note="n"),
            ),
            "new sid": (good(note="n"), good(sid=SID2), good(sid=SID2)),
            "both sids null": (good(sid=None, note="n"), good(sid=None), good(sid=None)),
            "a given note wins": (good(note="n"), good(note="m"), good(note="m")),
            "no old entry": (None, good(), good()),
        }
        for why, (old, value, want) in cases.items():
            with self.subTest(why):
                given = dict(value)
                with roster.locked(self.dir) as r:
                    r["entries"] = {} if old is None else {"w1": old}
                    roster.record(r, "w1", value)
                self.assertEqual(self.entries(), {"w1": want})
                self.assertEqual(value, given)

    def test_unwritten_prints_the_one_failure_line_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            roster.unwritten("w1", roster.RosterError("/d/roster.lock: busy"))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "manager: entry w1 not written: /d/roster.lock: busy\n")

    def test_remove_returns_the_entry_or_refuses_a_missing_one(self):
        with roster.locked(self.dir) as r:
            roster.set_entry(r, "w1", good())
        with roster.locked(self.dir) as r:
            self.assertEqual(roster.remove(r, "w1"), good())
            with self.assertRaises(roster.RosterError) as cm:
                roster.remove(r, "w1")
            self.assertEqual(str(cm.exception), "w1 not in roster")
        self.assertEqual(self.entries(), {})

    def test_recovery_forms_each_a_copy(self):
        resume = ["/usr/bin/python3", *MODULE, "start"]
        cases = [
            ("worker", SID, resume),
            ("pipeline", SID, resume),
            ("worker", None, resume),
            (
                "role",
                SID,
                [*resume, "--sid", SID, "--resume", "--input", "Continue the unfinished task."],
            ),
            ("role", None, None),
        ]
        for kind, sid, want in cases:
            with self.subTest(kind=kind, sid=sid):
                e = good(kind=kind, sid=sid, resume=list(resume))
                got = roster.recovery(e)
                self.assertEqual(got, want)
                if got is not None:
                    got.append("x")
                    self.assertEqual(e["resume"], resume)

    def test_recovery_runs_the_crew_module_for_a_crew_py_or_legacy_workers_py_entry(self):
        self.assertEqual(
            roster.CREW,
            os.path.realpath(
                os.path.join(SRC["manager"], "agent_pm", "manager", "engine", "crew.py")
            ),
        )
        self.assertTrue(os.path.isfile(roster.CREW))
        for script in ("/old/version/workers.py", "/old/skills/tmux/scripts/crew.py"):
            resume = ["/usr/bin/python3", script, "start", "w1"]
            for kind in ("worker", "pipeline", "role"):
                with self.subTest(script=script, kind=kind):
                    e = good(kind=kind, resume=list(resume))
                    got = roster.recovery(e)
                    self.assertEqual(got[:6], ["/usr/bin/python3", *MODULE, "start", "w1"])
                    self.assertEqual(e["resume"], resume)

    def test_recovery_gives_a_worker_without_a_client_the_default_runtime(self):
        self.local('runtime = "rt9"\n')
        head = ["/usr/bin/python3", *MODULE, "start", "w1"]
        legacy = ["/usr/bin/python3", "/x/crew.py", "start", "w1"]
        script = ["/usr/bin/python3", "/x/drive.py", "start", "w1"]
        rest = ["--manager", "m", "--resume", SID, "--cwd", "/w"]
        default = [*head, "--client", "rt9", *rest]
        cases = [
            ("worker", [*head, *rest], default),
            ("worker", [*legacy, *rest], default),
            ("worker", [*script, *rest], [*script, "--client", "rt9", *rest]),
            ("worker", [*head, *rest, "--", "--client", "x"], [*default, "--", "--client", "x"]),
            ("worker", [*head, "--client", "other", *rest], [*head, "--client", "other", *rest]),
            ("pipeline", [*script, *rest], [*script, *rest]),
            (
                "role",
                [*script, *rest],
                [
                    *script,
                    *rest,
                    "--sid",
                    SID,
                    "--resume",
                    "--input",
                    "Continue the unfinished task.",
                ],
            ),
        ]
        for kind, resume, want in cases:
            with self.subTest(kind=kind, resume=resume):
                e = good(kind=kind, resume=list(resume))
                self.assertEqual(roster.recovery(e), want)
                self.assertEqual(e["resume"], resume)

    def test_recovery_reads_the_config_only_for_a_worker_without_a_client(self):
        self.local('runtime = ""\n')
        head = ["/usr/bin/python3", *MODULE, "start", "w1"]
        script = ["/usr/bin/python3", "/x/router.py", "start", "w1"]
        for kind, resume in (
            ("worker", [*head, "--client", "other"]),
            ("worker", head[:5]),
            ("pipeline", script),
            ("role", script),
        ):
            with self.subTest(kind=kind, resume=resume):
                got = roster.recovery(good(kind=kind, resume=resume))
                self.assertEqual(got[: len(resume)], resume)
        with self.assertRaises(roster.RosterError) as cm:
            roster.recovery(good(resume=head))
        self.assertEqual(str(cm.exception), NO_RUNTIME)

    def test_printable(self):
        for text in ("", "plain text", "caf\xe9 \u65e5\u672c", "\xa0", "\u200b", "a b", "~"):
            with self.subTest(text=text):
                self.assertIs(roster.printable(text), expr2=True)
        for text in (
            "\n",
            "\r",
            "\t",
            "\x00",
            "\x1b",
            "\x7f",
            "\x80",
            "\x85",
            "\x9f",
            "\u2028",
            "\u2029",
            "a\nb",
        ):
            with self.subTest(text=text):
                self.assertIs(roster.printable(text), expr2=False)

    def test_sid(self):
        self.assertTrue(roster.SID.fullmatch(SID))
        for text in (SID.upper(), SID + "\n", SID[:-1], "", "; rm -rf ~", SID.replace("-", "")):
            with self.subTest(text=text):
                self.assertIsNone(roster.SID.fullmatch(text))

    def test_constants(self):
        self.assertEqual(
            (
                roster.VERSION,
                roster.NOTE_MAX,
                roster.ID_MAX,
                roster.LOCK_TIMEOUT,
                roster.ROSTER_MAX,
                roster.EVENTS_MAX,
                roster.SETTLE,
            ),
            (1, 500, 200, 30, 1 << 20, 1 << 20, 1),
        )
        self.assertEqual(roster.KINDS, ("worker", "role", "pipeline"))
        self.assertEqual(
            roster.STATES, ("working", "done", "blocked", "dead", "gone", "waiting", "finished")
        )
        self.assertEqual(
            (roster.ROOT, roster.WORKERS, roster.NAME.pattern),
            ("~/.agent-pm/managers", "~/.agent-pm/workers", "[A-Za-z0-9_-]+"),
        )
        self.assertFalse(hasattr(roster, "LEGACY_CLIENT"))
        for error in (roster.Held, roster.Stale):
            self.assertTrue(issubclass(error, roster.RosterError), error)
