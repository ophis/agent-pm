"""Controls behavior."""

import contextlib
import json
import os
from unittest import mock

from agent_pm.manager.engine import controls, roster
from agent_pm.session import (
    DriverError,
)

from manager.tests.unit.crew.case import RosterCase
from manager.tests.unit.crew.data import (
    MGR,
    OTHER,
    SID,
    SID2,
    UNPRINTABLE,
    SessionView,
    entry,
    nl,
    row,
    started,
)
from manager.tests.unit.crew.runtime import Api, Runs


class LeaseTest(RosterCase):
    CMDS = (["start", "w1"], ["next-event", "--after", "0", "--gen", "0"])
    FRESH = {"version": 1, "holder": None, "cursor": 0, "gen": 0, "entries": {}}

    def setUp(self):
        super().setUp()
        self.nxt = self.enterContext(
            mock.patch.object(
                self.event_source, "next", return_value=(1, "10:00:01 w1 outcome done")
            )
        )

    def rest(self, cmd):
        if not os.path.exists(self.path):
            return None
        r = json.loads(self.bytes())
        if cmd[0] == "start":
            del r["entries"]["w1"]
        return r

    def ran(self, fake, cmd):
        self.nxt.reset_mock()
        rc, _, err = self.main_with(fake, *cmd, "--manager", "mgr")
        err = "".join(x for x in err.splitlines(keepends=True) if not x.startswith("tui: "))
        return rc, err, bool(fake.starts) or self.nxt.called

    def test_refused_while_another_live_session_holds(self):
        before = self.seed(OTHER)
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(
                        self.ran(Api(own, [row("other")]), cmd), (1, self.held(), False)
                    )
                    self.assertEqual(self.bytes(), before)

    def test_refused_in_tmux_when_not_the_holder(self):
        for holder in (None, OTHER):
            before = self.seed(holder)
            for cmd in self.CMDS:
                with self.subTest(holder=holder, cmd=cmd[0]):
                    self.assertEqual(self.ran(Api(), cmd), (1, self.not_attached(), False))
                    self.assertEqual(self.bytes(), before)

    def test_refused_when_the_directory_is_missing_which_stays_missing(self):
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(self.ran(Api(own), cmd), (1, self.not_attached(), False))
                    self.assertEqual(self.made(), [])

    def test_allowed_for_the_holder(self):
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                before = json.loads(self.seed(MGR))
                self.assertEqual(self.ran(Api(), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), before)

    def test_allowed_outside_tmux_without_a_live_holder(self):
        roster.events(self.mdir)
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                self.assertEqual(self.ran(Api(None), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), self.FRESH)
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0], holder="dead"):
                before = json.loads(self.seed(OTHER))
                self.assertEqual(self.ran(Api(None), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), before)


class RosterCommandsTest(RosterCase):
    CMDS = (["release"], ["note", "w1", "x"], ["forget", "w1"], ["stop", "w1"])

    def test_refused_without_the_lease_writing_nothing(self):
        cases = {
            "another live holder": (OTHER, [row("other")], self.held()),
            "no holder": (None, [], self.not_attached()),
            "a dead other holder": (OTHER, [], self.not_attached()),
        }
        for why, (holder, rows, msg) in cases.items():
            for cmd in self.CMDS:
                with self.subTest(why, cmd=cmd[0]):
                    before = self.seed(holder, w1=entry())
                    fake = Api(rows=[*rows, row("w1", SID)])
                    self.assertEqual(self.main_with(fake, *cmd), (1, "", msg))
                    self.assertEqual(fake.ran("close"), [])
                    self.assertEqual(self.bytes(), before)

    def test_refused_when_the_directory_is_missing_which_stays_missing(self):
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(
                        self.main_with(Api(own), *cmd, "--manager", "mgr"),
                        (1, "", self.not_attached()),
                    )
                    self.assertEqual(self.made(), [])

    def test_session_free_commands_never_configure_and_work_with_a_bad_session_config(self):
        self.local('[hosts]\ndefault = "nope"\n')
        self.seed(MGR, w1=entry(), w2=entry(sid=SID2))
        for cmd in (["note", "w1", "x"], ["forget", "w2"], ["stop", "w1"], ["release"]):
            with self.subTest(cmd=cmd[0]):
                fake = Api(rows=[row("w1", SID)])
                self.assertEqual(self.main_with(fake, *cmd)[0], 0)

    def test_release_by_the_holder_keeps_the_cursor(self):
        self.seed(MGR, cursor=7, w1=entry())
        self.assertEqual(
            self.main_with(Api(), "release"), (0, f"workers: released {self.mdir}\n", "")
        )
        self.assertEqual(
            json.loads(self.bytes()),
            {"version": 1, "holder": None, "cursor": 7, "gen": 0, "entries": {"w1": entry()}},
        )

    def test_release_outside_tmux_without_a_live_holder(self):
        self.seed(OTHER, cursor=7)
        self.assertEqual(
            self.main_with(Api(None), "release", "--manager", "mgr"),
            (0, f"workers: released {self.mdir}\n", ""),
        )
        r = json.loads(self.bytes())
        self.assertEqual((r["holder"], r["cursor"]), (None, 7))

    def test_note_sets_and_an_empty_one_clears(self):
        self.seed(MGR, w1=entry(), w2=entry(note="old"), w3=entry())
        for argv in (["w1", "check the logs"], ["w2", ""], ["w3", "x" * 500]):
            self.assertEqual(self.main_with(Api(), "note", *argv), (0, "", ""))
        self.assertEqual(
            self.entries(),
            {"w1": entry(note="check the logs"), "w2": entry(), "w3": entry(note="x" * 500)},
        )

    def test_a_long_or_unprintable_note_is_refused_writing_nothing(self):
        cases = {
            "x" * 501: "note: 501 characters: want at most 500",
            **{
                text: f"note: {text!r}: {UNPRINTABLE}"
                for text in ("a\nb", "a\x9fb", "a b", "\x7f", "a\tb")
            },
        }
        for text, msg in cases.items():
            with self.subTest(msg):
                before = self.seed(MGR, w1=entry())
                self.assertEqual(
                    self.main_with(Api(), "note", "w1", text), (1, "", f"workers: {msg}\n")
                )
                self.assertEqual(self.bytes(), before)

    def test_note_forget_and_stop_need_an_entry_killing_nothing(self):
        for cmd in (["note", "w9", "x"], ["forget", "w9"], ["stop", "w9"]):
            with self.subTest(cmd=cmd[0]):
                before = self.seed(MGR, w1=entry())
                fake = Api(rows=[row("w9"), row("w1", SID)])
                self.assertEqual(self.main_with(fake, *cmd), (1, "", "workers: w9 not in roster\n"))
                self.assertEqual((self.bytes(), fake.ran("close")), (before, []))

    def test_note_forget_and_stop_check_the_name(self):
        for cmd in (["note", "a b", "x"], ["forget", "a b"], ["stop", "a b"]):
            with self.subTest(cmd=cmd[0]):
                before = self.seed(MGR, w1=entry())
                self.assertEqual(
                    self.main_with(Api(), *cmd),
                    (1, "", "workers: bad name 'a b': use [A-Za-z0-9_-]+\n"),
                )
                self.assertEqual(self.bytes(), before)

    def test_forget_removes_an_entry_no_session_keeps(self):
        self.seed(MGR, w1=entry(state="gone"), r1=entry("role", tui="r1-tui"), w2=entry())
        for name in ("w1", "r1"):
            self.assertEqual(self.main_with(Api(rows=[row("w2")]), "forget", name), (0, "", ""))
        self.assertEqual(self.entries(), {"w2": entry()})

    def test_forget_refuses_a_live_entry_writing_nothing(self):
        entries = {
            "w1": entry(),
            "r1": entry("role", tui="r1-tui"),
            "r2": entry("role", tui="r2-tui"),
            "p1": entry("pipeline", tui="p1-tui"),
        }
        fake = Api(rows=[row("w1"), row("r1"), row("r2-tui"), row("p1-tui")])
        for name, session in (("w1", "w1"), ("r1", "r1"), ("r2", "r2-tui"), ("p1", "p1-tui")):
            with self.subTest(name):
                before = self.seed(MGR, **entries)
                self.assertEqual(
                    self.main_with(fake, "forget", name),
                    (1, "", f"workers: {name}: session {session} is live\n"),
                )
                self.assertEqual(self.bytes(), before)

    def test_forget_after_unknown_sessions_writes_nothing(self):
        before = self.seed(MGR, w1=entry(state="gone"))
        fake = Api()
        fake.error, fake.error_after = DriverError("tmux sessions unknown: tmux failed"), 1
        self.assertEqual(
            self.main_with(fake, "forget", "w1"),
            (1, "", "workers: tmux sessions unknown: tmux failed\n"),
        )
        self.assertEqual(self.bytes(), before)


class StopTest(RosterCase):
    def stopped(self, fake, name, *argv):
        res = self.main_with(fake, "stop", name, *argv)
        return res, fake.ran("close"), set(self.entries())

    def test_a_worker(self):
        self.seed(MGR, w1=entry(), w2=entry(sid=SID2))
        fake = Api(rows=[row("w1", SID), row("w2", SID2)])
        self.assertEqual(self.stopped(fake, "w1"), ((0, "", ""), ["w1"], {"w2"}))
        self.assertEqual(set(fake.rows), {"mgr", "w2"})

    def test_a_role_or_pipeline_by_its_key_or_tui_its_driver_then_tui(self):
        for kind in ("role", "pipeline"):
            for name in ("d1", "t1"):
                with self.subTest(kind=kind, name=name):
                    self.seed(MGR, d1=entry(kind, tui="t1"), w1=entry())
                    fake = Api(rows=[row("d1"), row("t1"), row("w1", SID)])
                    self.assertEqual(self.stopped(fake, name), ((0, "", ""), ["d1", "t1"], {"w1"}))
                    self.assertEqual(set(fake.rows), {"mgr", "w1"})

    def test_sessions_gone_already(self):
        self.seed(
            MGR,
            w1=entry(state="gone"),
            d1=entry("role", tui="t1", state="finished"),
            d2=entry("role"),
            w2=entry(),
        )
        for name, closed, left in (
            ("w1", ["w1"], {"d1", "d2", "w2"}),
            ("t1", ["d1", "t1"], {"d2", "w2"}),
            ("d2", ["d2"], {"w2"}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.stopped(Api(), name), ((0, "", ""), closed, left))

    def test_a_session_outliving_its_close_exits_1_keeping_the_entry(self):
        before = self.seed(MGR, w1=entry())
        fake = Api(rows=[row("w1", SID)])
        fake.stuck.add("w1")
        self.assertEqual(
            self.main_with(fake, "stop", "w1"),
            (1, "", "workers: w1: session w1 still live after tmux kill-session\n"),
        )
        self.assertEqual(self.bytes(), before)

    def test_outside_tmux_without_a_live_holder(self):
        for holder in (None, OTHER):
            with self.subTest(holder=holder):
                self.seed(holder, w1=entry(), w2=entry(sid=SID2))
                fake = Api(None, [row("w1", SID)])
                self.assertEqual(
                    self.stopped(fake, "w1", "--manager", "mgr"), ((0, "", ""), ["w1"], {"w2"})
                )
                self.assertEqual(json.loads(self.bytes())["holder"], holder)

    def test_a_stopped_entry_is_not_resumed(self):
        self.seed(MGR, w1=entry(resume=started("w1")), w2=entry(sid=SID2, resume=started("w2")))
        self.assertEqual(self.main_with(Api(rows=[row("w1", SID)]), "stop", "w1"), (0, "", ""))
        self.assertEqual(self.main_with(Api(), "attach", "--resume")[0], 0)
        self.assertEqual([Runs.key(argv) for argv, _, _ in self.runs.recoveries], ["w2"])


class StopAllTest(RosterCase):
    def test_selects_by_opener_recursively_deepest_first(self):
        openers = {
            "mgr": None,
            "w1": "mgr",
            "w2": "mgr",
            "w3": "w1",
            "w4": "w3",
            "x": "other",
            "y": "x",
            "z": None,
            "loop": "loop",
            "back": "w4",
        }

        def live(**over):
            return {n: row(n, view=SessionView(opener=o)) for n, o in {**openers, **over}.items()}

        self.assertEqual(controls.opened("mgr", live()), ["back", "w4", "w3", "w1", "w2"])
        self.assertEqual(controls.opened("mgr", live(mgr="w1")), ["back", "w4", "w3", "w1", "w2"])
        self.assertEqual(controls.opened("nobody", live()), [])
        entries = {"mgr": {"x": entry(), "w9": entry(tui="t9"), "w1": entry()}}
        self.assertEqual(
            controls.opened("mgr", live(w9=None, t9="mgr"), lambda s: entries.get(s, {})),
            ["back", "w4", "w3", "t9", "w1", "w2", "w9"],
        )

    def rows(self):
        return [
            row("w1", SID, view=SessionView(pane="%1", opener="mgr")),
            row("w3", view=SessionView(pane="%2", opener="w1")),
            row("d1", view=SessionView(opener="mgr")),
            row("t1", view=SessionView(pane="%3", opener="mgr")),
            row("agent-pm-engineer-TASK-1", view=SessionView(opener="mgr")),
            row("o1", view=SessionView(pane="%4", opener="other")),
        ]

    def test_dry_run_lists_all_and_stops_nothing(self):
        before = self.seed(
            MGR,
            w1=entry(),
            d1=entry("role", tui="t1"),
            **{"agent-pm-engineer-TASK-1": entry("pipeline")},
        )
        fake = Api(rows=self.rows())
        self.assertEqual(
            self.main_with(fake, "stop-all", "--dry-run"),
            (
                0,
                nl(
                    "would stop w3",
                    "would stop agent-pm-engineer-TASK-1; a scheduler may resume it",
                    "would stop d1",
                    "would stop t1",
                    "would stop w1",
                ),
                "",
            ),
        )
        self.assertEqual((fake.ran("close"), self.bytes()), ([], before))

    def test_stops_them_and_removes_their_entries(self):
        self.seed(
            MGR,
            w1=entry(),
            d1=entry("role", tui="t1"),
            gone=entry(state="gone"),
            **{"agent-pm-engineer-TASK-1": entry("pipeline")},
        )
        w1 = os.path.join(self.agent_pm, "managers", "w1")
        roster.ensure(w1)
        roster.put(w1, "w3", entry())
        fake = Api(rows=self.rows())
        self.assertEqual(
            self.main_with(fake, "stop-all"),
            (
                0,
                nl(
                    "stopped w3",
                    "stopped agent-pm-engineer-TASK-1; a scheduler may resume it",
                    "stopped d1",
                    "stopped t1",
                    "stopped w1",
                ),
                "",
            ),
        )
        self.assertEqual(fake.ran("close"), ["w3", "agent-pm-engineer-TASK-1", "d1", "t1", "w1"])
        self.assertEqual((set(fake.rows), set(self.entries())), ({"mgr", "o1"}, {"gone"}))
        with roster.locked(w1, write=False) as r:
            self.assertEqual(r["entries"], {})

    def test_unshown_sessions_of_its_roster_too_an_entry_kept_while_its_key_lives(self):
        self.seed(MGR, w1=entry(), d1=entry("role", tui="t1"))
        fake = Api(rows=[row("w1", SID), row("d1"), row("t1")])
        fake.stuck.add("d1")
        live = "workers: stop-all: still live after tmux kill-session: d1\n"
        self.assertEqual(self.main_with(fake, "stop-all"), (1, "stopped t1\nstopped w1\n", live))
        self.assertEqual(set(self.entries()), {"d1"})

    def test_nothing_to_stop(self):
        self.assertEqual(
            self.main_with(Api(rows=[row("o1", view=SessionView(opener="other"))]), "stop-all"),
            (0, "workers: stop-all: nothing to stop\n", ""),
        )

    def test_a_session_outliving_its_close_exits_1_keeping_its_entry(self):
        self.seed(MGR, w1=entry(), w2=entry())
        fake = Api(
            rows=[
                row("w1", view=SessionView(opener="mgr")),
                row("w2", view=SessionView(opener="mgr")),
            ]
        )
        fake.stuck.add("w1")
        self.assertEqual(
            self.main_with(fake, "stop-all"),
            (1, "stopped w2\n", "workers: stop-all: still live after tmux kill-session: w1\n"),
        )
        self.assertEqual(set(self.entries()), {"w1"})

    def test_outside_tmux_or_not_the_holder_stops_nothing(self):
        self.assertEqual(
            self.main_with(Api(None, [row("w1", view=SessionView(opener="mgr"))]), "stop-all"),
            (1, "", "workers: stop-all: not in tmux: run it in the manager's tmux session\n"),
        )
        self.seed(OTHER, w1=entry())
        fake = Api(rows=[row("w1", view=SessionView(opener="mgr")), row("other")])
        self.assertEqual(self.main_with(fake, "stop-all"), (1, "", self.held()))
        self.assertEqual(fake.ran("close"), [])
